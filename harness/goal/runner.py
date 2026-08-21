"""Background Goal runner using Task-owned contracts and evidence."""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.agent.cancel import clear_cancel, request_cancel
from harness.agents.runner import AgentTaskStats, run_agent_task
from harness.evaluation import run_task_evaluation
from harness.goal.engine import GoalEngine
from harness.goal.language import human_language_label
from harness.goal.models import GoalPhase, GoalStatus, GoalState, StopReason
from harness.goal.planner import VerificationSpec, plan_tasks
from harness.goal.policy import MAX_REPAIR_ATTEMPTS_PER_TASK, NO_PROGRESS_REPLAN_LIMIT, validate_limits
from harness.goal.supervision import StagePolicy, StageProgress, StageSupervisor, emit_stage_supervision
from harness.goal.store import (
    GoalLeaseError,
    GoalStoreError,
    acquire_goal_lease,
    archive_goal,
    load_goal,
    release_goal_lease,
    save_goal,
)
from harness.settings import get_workdir, workspace_generation
from harness.verification import (
    VerificationContext, build_pytest_command, collect_pytest_catalog,
    reverify_task_command, run_verification, select_adapter, verify_task_command,
)
from harness.verification.evidence import evidence_from_result

class GoalNotRunningError(Exception):
    pass


class GoalBusyError(Exception):
    pass


# Unlike the read-only planner, a test writer must be able to inspect existing
# conventions, write a new test, then submit the selector JSON on a final turn.
TEST_WRITER_MAX_ROUNDS = 8
TEST_WRITER_MAX_IDLE_CHUNKS = 3


def _execution_workspace(state: GoalState) -> str:
    """Return the scoped project root used for code and test operations."""
    root = Path(state.workspace).expanduser().resolve()
    try:
        candidate = Path(state.execution_workspace or root).expanduser().resolve()
    except OSError:
        return str(root)
    return str(candidate) if candidate.is_dir() and candidate.is_relative_to(root) else str(root)


@dataclass(frozen=True)
class GoalRequest:
    target: str
    verification: str
    execution_workspace: str = ""
    # A user-approved draft can seed the durable Task plan. The runner still
    # owns test generation, baselines, implementation, and every completion gate.
    task_plan: list[dict[str, Any]] | None = None
    goal_contract: dict[str, Any] | None = None
    draft_id: str = ""
    await_execution_approval: bool = False
    worker_round_limit: int = 20
    operation_timeout_seconds: int = 1800
    evaluation_required: bool = True


_local = threading.local()


def is_goal_noninteractive() -> bool:
    return bool(getattr(_local, "noninteractive", False))


def goal_permission_pending() -> bool:
    return bool(getattr(_local, "permission_pending", False))


def mark_goal_permission_pending() -> None:
    _local.permission_pending = True


def clear_goal_permission_flags() -> None:
    _local.permission_pending = False
    _local.noninteractive = False


def set_goal_noninteractive(active: bool) -> None:
    _local.noninteractive = active


def _emit(event_type: str, **payload: Any) -> None:
    from harness.ui import events

    if events.is_enabled():
        events.emit(event_type, **payload)


def goal_event_payload(state: GoalState) -> dict[str, Any]:
    """Build the TUI snapshot from persisted Goal and Task state.

    The terminal must render the same contracts that gate completion, instead
    of inferring progress from model prose or tool-log ordering.
    """
    from harness.tasks import load_task

    tasks: list[dict[str, Any]] = []
    for task_id in state.task_ids:
        try:
            task = load_task(task_id)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            tasks.append(
                {
                    "id": task_id,
                    "subject": "Task state unavailable",
                    "status": "missing",
                    "verification_state": "unknown",
                    "blocked_by": [],
                    "acceptance_cases": [],
                    "verification_spec": {},
                    "evidence_count": 0,
                    "latest_evidence": None,
                    "last_error": "Task state could not be loaded",
                }
            )
            continue
        evidence_items = task.evidence if isinstance(task.evidence, list) else []
        latest_evidence = evidence_items[-1] if evidence_items and isinstance(evidence_items[-1], dict) else None
        evidence_summary = None
        if latest_evidence is not None:
            evidence_selectors = latest_evidence.get("selectors")
            evidence_summary = {
                "command": str(latest_evidence.get("command") or ""),
                "exit_code": latest_evidence.get("exit_code"),
                "stdout_tail": str(latest_evidence.get("stdout_tail") or "")[-1600:],
                "duration_ms": latest_evidence.get("duration_ms"),
                "verified_by": str(latest_evidence.get("verified_by") or ""),
                "code_snapshot": str(latest_evidence.get("code_snapshot") or ""),
                "selectors": list(evidence_selectors) if isinstance(evidence_selectors, (list, tuple)) else [],
                "collected_count": latest_evidence.get("collected_count") or 0,
            }
        tasks.append(
            {
                "id": task.id,
                "subject": task.subject,
                "status": task.status,
                "verification_state": task.verification_state,
                "blocked_by": list(task.blockedBy) if isinstance(task.blockedBy, list) else [],
                "acceptance_cases": task.acceptance_cases if isinstance(task.acceptance_cases, list) else [],
                "skills": list(task.skill_names) if isinstance(task.skill_names, list) else [],
                "scope_paths": list(task.scope_paths) if isinstance(task.scope_paths, list) else [],
                "evidence_refs": list(task.evidence_refs) if isinstance(task.evidence_refs, list) else [],
                "test_strategy": str(task.test_strategy or ""),
                "verification_spec": task.verification_spec if isinstance(task.verification_spec, dict) else {},
                "evidence_count": len(evidence_items),
                "latest_evidence": evidence_summary,
                "last_error": task.last_error,
            }
        )
    return {
        "id": state.id,
        "target": state.target,
        "verification": state.verification,
        "phase": state.phase,
        "status": state.status,
        "draft_id": state.draft_id,
        "current_task_id": state.current_task_id,
        "resume_phase": state.resume_phase,
        "execution_approved": state.execution_approved,
        "task_cycles": state.attempts,
        "total_llm_rounds": state.total_llm_rounds,
        "worker_generation": state.worker_generation,
        "worker_rollovers": state.worker_rollovers,
        "worker_round_limit": state.worker_round_limit,
        "updated_at": state.updated_at,
        "paused_at": state.paused_at,
        "stop_reason": state.stop_reason,
        "final_verification": dict(state.final_verification) if isinstance(state.final_verification, dict) else None,
        "last_error": state.last_error,
        "tasks": tasks,
    }


def _emit_goal(event_type: str, state: GoalState, **metadata: Any) -> None:
    _emit(event_type, **goal_event_payload(state), **metadata)


def emit_current_goal_status(*, include_terminal: bool = True, hydrated: bool = False) -> GoalState | None:
    """Emit the persisted Goal snapshot for startup hydration or `/goal status`."""
    try:
        state = load_goal()
    except GoalStoreError as exc:
        _emit("goal_status_error", code=exc.code, error=str(exc))
        return None
    if state is None:
        return None
    terminal = {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}
    if include_terminal or state.status not in terminal:
        if hydrated:
            _emit_goal("goal_status", state, hydrated=True)
        else:
            _emit_goal("goal_status", state)
    return state


def format_goal_status(state: GoalState) -> str:
    lines = [f"Goal {state.id} [{state.phase}] ({state.status})", f"  Target: {state.target[:80]}"]
    if state.task_ids:
        from harness.tasks import load_task

        completed = 0
        lines.append(f"  Tasks: {len(state.task_ids)}")
        for task_id in state.task_ids:
            try:
                task = load_task(task_id)
                mark = "x" if task.status == "completed" else (">" if task_id == state.current_task_id else ".")
                completed += task.status == "completed"
                lines.append(f"    {mark} {task.id} [{task.status}/{task.verification_state}] {task.subject[:48]}")
            except FileNotFoundError:
                lines.append(f"    ! {task_id} [missing]")
        lines.append(f"  Completed: {completed}/{len(state.task_ids)}")
    if state.phase == GoalPhase.PREPARE_TESTS.value:
        lines.append("  Waiting for Task test generation and test collection.")
    lines += [
        f"  Task cycles: {state.attempts}",
        f"  Workers: {state.worker_generation} ({state.worker_rollovers} rollover(s), limit {state.worker_round_limit} rounds each)",
        f"  Goal elapsed: {int(max(0, time.time() - state.started_at))}s (unbounded)",
    ]
    if state.last_error:
        lines.append(f"  Last error: {state.last_error[:200]}")
    if state.stop_reason:
        lines.append(f"  Stop reason: {state.stop_reason}")
    return "\n".join(lines)


_runner: "GoalRunner | None" = None
_runner_lock = threading.Lock()


def _reap_runner() -> None:
    global _runner
    with _runner_lock:
        if _runner is not None and (not _runner.is_alive() or not _runner.is_running()):
            _runner = None


def is_goal_running() -> bool:
    with _runner_lock:
        return _runner is not None and _runner.is_alive() and _runner.is_running()


def get_goal_status() -> str:
    # Drafts are the active user workflow. A stale terminal Goal must never
    # hide an in-progress clarification/discovery/planning draft.
    try:
        from harness.goal.draft import format_draft, load_draft

        draft = load_draft()
        if draft is not None and draft.status != "consumed":
            state = load_goal()
            if state is not None and state.draft_id == draft.id:
                from harness.goal.draft import mark_draft_consumed

                mark_draft_consumed()
                return format_goal_status(state)
            return format_draft(draft)
    except Exception:
        # Goal status should still be useful when a draft file is corrupt; the
        # caller will get the normal Goal state error below.
        pass
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            return format_goal_status(runner._state)
    try:
        state = load_goal()
    except GoalStoreError as exc:
        return f"Goal state is {exc.code}: {exc}"
    return format_goal_status(state) if state else "No goal in this workspace."


def start_goal(request: GoalRequest, *, history: list, context: dict, binding: Any) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        existing = load_goal()
        if _runner is not None and _runner.is_alive() and _runner.is_running():
            raise GoalBusyError("A goal is already running. Use /goal status, pause, or cancel.")
        if existing and existing.status not in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
            raise GoalBusyError(f"Goal {existing.id} is {existing.status}. Resume, cancel, or finish it first.")
        if existing:
            archive_goal(existing)
        workspace_root = get_workdir().resolve()
        try:
            execution_workspace = Path(request.execution_workspace or workspace_root).expanduser().resolve()
        except OSError as exc:
            raise ValueError(f"invalid Goal execution workspace: {exc}") from exc
        if not execution_workspace.is_dir() or not execution_workspace.is_relative_to(workspace_root):
            raise ValueError("Goal execution workspace must be an existing directory inside the current workspace")
        state = GoalState.new(
            target=request.target, verification=request.verification, workspace=str(workspace_root),
            execution_workspace=str(execution_workspace),
            workspace_generation=workspace_generation(), evaluation_required=request.evaluation_required,
            worker_round_limit=request.worker_round_limit,
            operation_timeout_seconds=request.operation_timeout_seconds,
            draft_id=request.draft_id,
        )
        if request.task_plan:
            state.task_plan = [dict(item) for item in request.task_plan]
        state.goal_contract = dict(request.goal_contract or {
            "target": request.target,
            "autonomy": "After /goal run, resolve ordinary requirement ambiguity from the contract and repository evidence without asking the user.",
        })
        state.execution_approved = not request.await_execution_approval
        problems = validate_limits(state)
        if problems:
            raise ValueError("invalid goal limits: " + "; ".join(problems))
        try:
            lease_token = acquire_goal_lease(state)
        except GoalLeaseError as exc:
            raise GoalBusyError(str(exc)) from exc
        try:
            save_goal(state)
            # pause/cancel shares the legacy agent cancellation signal.  A
            # previous Goal must not leave the next one born cancelled.
            clear_cancel()
            _runner = GoalRunner(
                state=state, history=history, context=context, binding=binding,
                lease_token=lease_token,
            )
            _emit_goal("goal_started", state)
            _runner.start()
            return state
        except BaseException:
            _runner = None
            release_goal_lease(state, lease_token)
            raise


def resume_goal(*, history: list, context: dict, binding: Any, approve_execution: bool = False) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        if _runner is not None and _runner.is_alive() and _runner.is_running():
            raise GoalBusyError("A goal is already running. Use /goal status, pause, or cancel.")
        state = load_goal()
        if state is not None and state.status in {GoalStatus.RUNNING.value, GoalStatus.PAUSING.value, GoalStatus.CANCELLING.value}:
            raise GoalBusyError("A Goal runner is already active for this workspace.")
        if state is None or state.status != GoalStatus.PAUSED.value:
            raise GoalNotRunningError("No paused goal to resume.")
        if Path(state.workspace).expanduser().resolve() != get_workdir().resolve():
            raise GoalNotRunningError(
                "Paused Goal belongs to a different workspace. Switch back to its workspace before resuming."
            )
        try:
            lease_token = acquire_goal_lease(state)
        except GoalLeaseError as exc:
            raise GoalBusyError(str(exc)) from exc
        try:
            # Never write a task or Goal checkpoint before this process owns
            # the lease.  A second TUI used to cancel a live runner's legacy
            # repair task before discovering the lease conflict.
            _discard_legacy_interrupted_final_repair(state)
            _discard_unbound_goal_regression_repair(state)
            if state.stop_reason == StopReason.user_approval_required.value:
                if not approve_execution:
                    raise GoalNotRunningError("Goal is waiting for /goal run. Ordinary /goal resume cannot approve implementation.")
                state.execution_approved = True
            elif approve_execution:
                raise GoalNotRunningError("No Goal is waiting for execution approval.")

            # Pauses are outside the active execution budget. This also makes
            # a draft safely resumable the next day after test review.
            if state.paused_at is not None:
                state.started_at += max(0.0, time.time() - state.paused_at)
                state.paused_at = None

            target = _resume_target(state)
            GoalEngine().transition(state, target, "goal_resumed")
            state.stop_reason = None
            save_goal(state)
            clear_cancel()
            _runner = GoalRunner(
                state=state, history=history, context=context, binding=binding,
                lease_token=lease_token,
            )
            _emit_goal("goal_started", state)
            _runner.start()
            return state
        except BaseException:
            _runner = None
            release_goal_lease(state, lease_token)
            raise


def _resume_target(state: GoalState) -> str:
    """Recover only to a phase whose durable prerequisites still hold."""
    from harness.tasks import load_task

    if not state.initialization_complete or len(state.task_name_ids) < len(state.task_plan):
        return GoalPhase.INITIALIZE.value
    try:
        tasks = {task_id: load_task(task_id) for task_id in state.task_ids}
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return GoalPhase.INITIALIZE.value
    if not state.execution_approved:
        return GoalPhase.PREPARE_TESTS.value
    candidate = state.resume_phase or GoalPhase.SELECT_TASK.value
    allowed = {
        phase.value for phase in (
            GoalPhase.SELECT_TASK, GoalPhase.CLAIM, GoalPhase.ACT,
            GoalPhase.ROLLOVER, GoalPhase.VERIFY, GoalPhase.EVALUATE, GoalPhase.REPAIR_PLAN,
            GoalPhase.IMPACT_REVIEW, GoalPhase.CLEAN_CHECK, GoalPhase.FULL_VERIFY,
        )
    }
    if candidate not in allowed:
        return GoalPhase.SELECT_TASK.value

    # A paused Task checkpoint wins over unrelated future Tasks that are still
    # waiting for their dependencies.  Otherwise a review pause on Task A is
    # incorrectly routed through test generation for Task B and returns to ACT.
    current = tasks.get(state.current_task_id or "")
    task_phases = {
        GoalPhase.CLAIM.value,
        GoalPhase.ACT.value,
        GoalPhase.ROLLOVER.value,
        GoalPhase.VERIFY.value,
        GoalPhase.EVALUATE.value,
        GoalPhase.REPAIR_PLAN.value,
        GoalPhase.CLEAN_CHECK.value,
        GoalPhase.IMPACT_REVIEW.value,
    }
    if candidate in task_phases:
        if current is None:
            return GoalPhase.SELECT_TASK.value
        if current.status == "completed":
            # Task archival happens before the impact-review checkpoint. A
            # restart in that small window must still review downstream tests.
            return GoalPhase.IMPACT_REVIEW.value if candidate == GoalPhase.CLEAN_CHECK.value else GoalPhase.SELECT_TASK.value
        if current.verification_state == "needs_generation":
            return GoalPhase.PREPARE_TESTS.value
        if candidate == GoalPhase.EVALUATE.value and current.verification_state != "passing":
            return GoalPhase.VERIFY.value
        if candidate == GoalPhase.REPAIR_PLAN.value and current.verification_state == "passing":
            from harness.verification.snapshot import capture_code_snapshot

            evaluation = current.evaluation if isinstance(current.evaluation, dict) else {}
            evaluated_snapshot = str(evaluation.get("input_snapshot") or "")
            # An old or changed evaluation input cannot safely direct a new
            # implementation attempt. Re-evaluate the current workspace first.
            if not evaluated_snapshot or evaluated_snapshot != capture_code_snapshot(_execution_workspace(state)):
                return GoalPhase.EVALUATE.value
        if candidate in {GoalPhase.VERIFY.value, GoalPhase.CLEAN_CHECK.value} and current.status != "in_progress":
            return GoalPhase.CLAIM.value if current.status == "pending" else GoalPhase.SELECT_TASK.value
        if candidate in {GoalPhase.ACT.value, GoalPhase.ROLLOVER.value, GoalPhase.REPAIR_PLAN.value} and current.status == "pending":
            return GoalPhase.CLAIM.value
        return candidate

    # SELECT_TASK will find the first runnable test-generation task.  Do not
    # preempt the saved checkpoint merely because a blocked future Task exists.
    return candidate


def _discard_legacy_interrupted_final_repair(state: GoalState) -> bool:
    """Remove the old synthetic repair Task created for a cancelled pytest run.

    Older runners treated pytest's exit code 2 (interrupted) as a product
    regression and queued an untestable ``goal regression repair`` Task.  The
    Task file remains as an auditable cancelled record; only the Goal queue is
    repaired so resume can rerun the final verification.
    """
    from harness.goal.memory import remove_test_bindings
    from harness.tasks import load_task, save_task

    final = state.final_verification if isinstance(state.final_verification, dict) else {}
    output = "\n".join(
        str(final.get(field) or "")
        for field in ("stdout_tail", "stderr_tail", "error")
    ).lower()
    if final.get("exit_code") != 2 or "keyboardinterrupt" not in output:
        return False
    task_id = state.current_task_id
    if not task_id:
        return False
    try:
        task = load_task(task_id)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    task_detail = "\n".join(
        [task.subject, task.description, str((task.evaluation or {}).get("summary") or "")]
    ).lower()
    if not task.subject.startswith("goal regression repair ") or "exit code 2" not in task_detail:
        return False

    # The historical runner did not record provenance.  Only remove a test
    # file when its current digest still equals the synthetic Task's immutable
    # binding digest; a user-edited file is deliberately preserved.
    spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
    expected_hashes = spec.get("test_hashes") if isinstance(spec.get("test_hashes"), dict) else {}
    workspace = Path(_execution_workspace(state)).resolve()
    for raw_path, expected_hash in expected_hashes.items():
        rel = str(raw_path).replace("\\", "/")
        candidate = (workspace / rel).resolve()
        if (
            not candidate.is_relative_to(workspace)
            or candidate.name.lower() == "conftest.py"
            or not candidate.name.lower().startswith("test")
        ):
            continue
        try:
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == str(expected_hash):
                candidate.unlink()
        except OSError:
            continue
    remove_test_bindings(state, task.id)
    task.status = "cancelled"
    task.last_error = "Superseded: full verification was interrupted, not a code regression."
    save_task(task)
    state.task_ids = [candidate for candidate in state.task_ids if candidate != task_id]
    state.task_name_ids = {
        name: candidate for name, candidate in state.task_name_ids.items() if candidate != task_id
    }
    state.task_plan = [
        plan for plan in state.task_plan
        if not (str(plan.get("name") or "") == task.subject)
    ]
    state.current_task_id = None
    state.resume_phase = GoalPhase.FULL_VERIFY.value
    state.final_verification = {
        **final,
        "status": "interrupted",
        "error": "Full verification was interrupted before completion; retrying is required.",
        "updated_at": time.time(),
    }
    state.last_error = "Full verification was interrupted before completion; /goal resume will retry it."
    state.stop_reason = StopReason.full_verification_interrupted.value
    return True


def _discard_unbound_goal_regression_repair(state: GoalState) -> bool:
    """Discard an untouched synthetic final-regression Task on resume.

    Whole-suite failures must reopen their owning Task or pause for review.
    They must never create another numbered ``goal regression repair`` Task,
    whether an old runner left it unbound or a newer runner bound it directly.
    """
    from harness.goal.memory import remove_test_bindings
    from harness.tasks import load_task, save_task

    task_id = state.current_task_id
    if not task_id:
        return False
    try:
        task = load_task(task_id)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
    if (
        not task.subject.startswith("goal regression repair ")
        or task.status not in {"pending", "in_progress"}
        or (task.status == "in_progress" and task.owner != f"goal:{state.id}")
        or spec.get("goal_regression_fingerprint")
    ):
        return False
    task.status = "cancelled"
    task.last_error = "Superseded: whole-suite failures never create a synthetic Goal Task."
    save_task(task)
    remove_test_bindings(state, task.id)
    state.task_ids = [candidate for candidate in state.task_ids if candidate != task.id]
    state.task_name_ids = {
        name: candidate for name, candidate in state.task_name_ids.items() if candidate != task.id
    }
    state.task_plan = [plan for plan in state.task_plan if str(plan.get("name") or "") != task.subject]
    state.current_task_id = None
    state.resume_phase = GoalPhase.FULL_VERIFY.value
    state.last_error = "Retrying full verification without creating a synthetic Goal Task."
    return True


def pause_goal() -> GoalState:
    with _runner_lock:
        if _runner is None:
            raise GoalNotRunningError("Goal is not running.")
        return _runner.request_pause()


def cancel_goal() -> GoalState:
    with _runner_lock:
        if _runner is not None:
            return _runner.request_cancel()
        state = load_goal()
        if state is None or state.status != GoalStatus.PAUSED.value:
            raise GoalNotRunningError("Goal is not running or paused.")
        try:
            lease_token = acquire_goal_lease(state)
        except GoalLeaseError as exc:
            raise GoalBusyError(str(exc)) from exc
        try:
            GoalEngine().transition(
                state,
                GoalPhase.CANCELLED,
                StopReason.cancelled_by_user.value,
                error="user requested cancel",
                stop_reason=StopReason.cancelled_by_user.value,
            )
            save_goal(state)
            archive_goal(state)
            _emit_goal("goal_stopped", state)
            return state
        finally:
            release_goal_lease(state, lease_token)


class GoalRunner(threading.Thread):
    def __init__(
        self, *, state: GoalState, history: list, context: dict, binding: Any,
        lease_token: str | None = None,
    ):
        super().__init__(name=f"goal-{state.id}", daemon=True)
        self._state, self._history, self._context, self._binding = state, history, context, binding
        self._lock = threading.RLock()
        self._pause_event, self._cancel_event = threading.Event(), threading.Event()
        self._phase_in_flight = False
        self._archived = False
        self._lease_token = lease_token
        self.engine = GoalEngine()

    def is_running(self) -> bool:
        return self._state.status in {GoalStatus.RUNNING.value, GoalStatus.PAUSING.value, GoalStatus.CANCELLING.value}

    def request_pause(self) -> GoalState:
        if self._state.status != GoalStatus.RUNNING.value:
            raise GoalNotRunningError("Goal is not running.")
        self._state.status = GoalStatus.PAUSING.value
        save_goal(self._state)
        _emit_goal("goal_phase", self._state)
        self._pause_event.set()
        request_cancel()
        return self._state

    def request_cancel(self) -> GoalState:
        if not self.is_running():
            raise GoalNotRunningError("Goal is not running.")
        self._state.status = GoalStatus.CANCELLING.value
        self._cancel_event.set()
        request_cancel()
        save_goal(self._state)
        _emit_goal("goal_phase", self._state)
        return self._state

    def run(self) -> None:
        try:
            self._drive()
        except Exception as exc:
            self._fail(self._state, StopReason.internal_error, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if self._state.status in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                    archive_goal(self._state)
            finally:
                release_goal_lease(self._state, self._lease_token)
            _emit_goal("goal_stopped", self._state)

    def _drive(self) -> None:
        while self.is_running():
            state = self._state
            if workspace_generation() != state.workspace_generation:
                self._fail(state, StopReason.workspace_changed, "workspace switched while goal was active")
                return
            if self._cancel_event.is_set():
                self._cancel(state, "user requested cancel")
                return
            if self._pause_event.is_set() and not self._phase_in_flight:
                self._pause(state, "user_pause")
                return
            self._step_once(state)

    def _step_once(self, state: GoalState) -> None:
        if state.phase == GoalPhase.INITIALIZE.value:
            self._initialize(state)
        elif state.phase == GoalPhase.PREPARE_TESTS.value:
            self._prepare_tests(state)
        elif state.phase == GoalPhase.SELECT_TASK.value:
            self._select_task(state)
        elif state.phase == GoalPhase.CLAIM.value:
            self._claim(state)
        elif state.phase == GoalPhase.ACT.value:
            self._act(state)
        elif state.phase == GoalPhase.ROLLOVER.value:
            self._rollover(state)
        elif state.phase == GoalPhase.VERIFY.value:
            self._verify(state)
        elif state.phase == GoalPhase.EVALUATE.value:
            self._evaluate(state)
        elif state.phase == GoalPhase.REPAIR_PLAN.value:
            self._repair_plan(state)
        elif state.phase == GoalPhase.IMPACT_REVIEW.value:
            self._impact_review(state)
        elif state.phase == GoalPhase.FULL_VERIFY.value:
            self._full_verify(state)
        elif state.phase == GoalPhase.CLEAN_CHECK.value:
            self._clean_check(state)
        else:
            raise GoalNotRunningError(f"cannot step from {state.phase}")

    def _initialize(self, state: GoalState) -> None:
        from harness.goal.memory import load_test_map, record_test_binding
        from harness.tasks import create_task, list_tasks, load_task, save_task

        root = Path(_execution_workspace(state))
        if not state.task_plan:
            stats = AgentTaskStats()
            self._phase_in_flight = True
            try:
                try:
                    plans = plan_tasks(
                        state.target, state.verification, root,
                        cancel_check=self._interrupted, deadline=self._deadline(state), stats=stats,
                        human_language=human_language_label((state.goal_contract or {}).get("language")),
                    )
                except Exception as exc:
                    state.total_llm_rounds += stats.llm_rounds
                    state.last_error = f"Goal planner unavailable: {type(exc).__name__}: {exc}"
                    self._pause(
                        state,
                        "goal_planner_unavailable",
                        stop_reason=StopReason.provider_unavailable.value
                        if stats.stop_reason in {"provider_error", "configuration_error"}
                        else StopReason.provider_unavailable.value,
                    )
                    return
            finally:
                self._phase_in_flight = False
            state.total_llm_rounds += stats.llm_rounds
            if self._honor_control_request(state):
                return
            state.task_plan = [item.to_dict() for item in plans]
            save_goal(state)
        plans = state.task_plan
        # Task creation is deliberately idempotent. A process may stop after
        # writing one Task but before writing the next Goal checkpoint.
        names = dict(state.task_name_ids)
        plan_names = {str(plan.get("name")) for plan in plans}
        # Recover a Task written just before a process crash, before its Goal
        # checkpoint could record the id. Limit reconciliation to this Goal.
        for existing in list_tasks(include_archived=True):
            if existing.goal_id == state.id and existing.subject in plan_names:
                names.setdefault(existing.subject, existing.id)
        for task_id in state.task_ids:
            try:
                existing = load_task(task_id)
            except (FileNotFoundError, OSError, TypeError, ValueError):
                continue
            # ``state.task_ids`` is an explicit durable association from older
            # Goal files, which predate Task.goal_id. Trust it for migration;
            # unreferenced board scanning above remains limited to this Goal.
            if existing.subject in plan_names:
                names.setdefault(existing.subject, existing.id)
        for plan in plans:
            name = str(plan["name"])
            if name in names:
                continue
            deps = [names[dep] for dep in plan.get("depends_on", [])]
            task = create_task(
                name, plan["behavior"], deps, goal_id=state.id,
                acceptance_cases=list(plan.get("acceptance_cases") or []),
                skill_names=list(plan.get("skills") or []),
                verification_spec=dict(plan.get("verification_spec") or {}),
                evaluation_required=state.evaluation_required,
                scope_paths=list(plan.get("scope_paths") or []),
                evidence_refs=list(plan.get("evidence_refs") or []),
                test_strategy=str(plan.get("test_strategy") or ""),
                discovery_revision=int(plan.get("discovery_revision") or 0),
            )
            names[name] = task.id
            state.task_name_ids = dict(names)
            state.task_ids = [names[str(item["name"])] for item in plans if str(item["name"]) in names]
            save_goal(state)
        state.task_name_ids = dict(names)
        state.task_ids = [names[str(item["name"])] for item in plans if str(item["name"]) in names]
        known_bindings: set[str] = set()
        for entry in load_test_map(state):
            if isinstance(entry, dict):
                known_bindings.update(str(task_id) for task_id in entry.get("task_ids", []))
        for task_id in state.task_ids:
            task = load_task(task_id)
            spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
            if spec.get("source") != "discovered" or task_id in known_bindings:
                continue
            spec["owners"] = [task_id]
            # Completion depends on explicit case->selector evidence. Never
            # manufacture coverage merely because a selector exists.
            spec["covers"] = list(spec.get("case_selectors") or [])
            if not spec.get("test_hashes"):
                spec["test_hashes"] = self._test_file_hashes(root, spec.get("test_files") or [])
            task.verification_spec = spec
            save_task(task)
            record_test_binding(state, task, spec)
        state.initialization_complete = True
        state.current_task_id = state.task_ids[0] if state.task_ids else None
        save_goal(state)
        if self._needs_test_generation(state):
            self._apply(state, GoalPhase.PREPARE_TESTS, "task_tests_required")
        elif not state.execution_approved:
            self._pause(
                state,
                "plan_ready_for_user_approval",
                stop_reason=StopReason.user_approval_required.value,
            )
        else:
            self._apply(state, GoalPhase.SELECT_TASK, "initialize_complete")

    def _needs_test_generation(self, state: GoalState) -> bool:
        from harness.tasks import load_task

        return any(load_task(task_id).verification_state == "needs_generation" for task_id in state.task_ids)

    def _prepare_tests(self, state: GoalState) -> None:
        """Generate and bind tests before any implementation Task is selected."""
        from harness.goal.memory import record_test_binding
        from harness.tasks import bind_task_verification, load_task

        root = Path(_execution_workspace(state))
        deferred = False
        for task_id in state.task_ids:
            task = load_task(task_id)
            if task.verification_state != "needs_generation":
                continue
            # A dependent Task's red baseline is meaningful only after its
            # prerequisites exist. Otherwise Task A's absence can be falsely
            # attributed to Task B.
            if any(load_task(dep).status != "completed" for dep in task.blockedBy):
                deferred = True
                continue
            adapter = select_adapter(root, task.verification_spec.get("adapter") or state.verification)
            verification_context = VerificationContext(root, command=state.verification)
            before_catalog = (
                collect_pytest_catalog(root)
                if adapter.id == "pytest"
                else adapter.discover(verification_context)
            )
            write_roots = self._test_write_roots(before_catalog)
            before_tree = self._snapshot_test_tree(root, write_roots)
            impact_context = task.verification_spec.get("impact_context") or []
            if not isinstance(impact_context, list):
                impact_context = []
            prompt = (
                f"Create a NEW focused {adapter.id} test file for this Task before implementation. You may modify only test files; "
                "do not edit existing test files or production code. Use existing test conventions. After writing tests, reply ONLY with JSON: "
                '{"test_selectors":["tests/test_x.py::test_name"],"case_selectors":{"AC1":["tests/test_x.py::test_name"]}}.\n\n'
                f"Task: {task.subject}\nBehavior: {task.description}\nAcceptance cases: {json.dumps(task.acceptance_cases)}"
            )
            prompt += (
                "\n\nWrite only human-facing test descriptions and your final summary in "
                f"{human_language_label((state.goal_contract or {}).get('language'))}. "
                "Keep JSON keys, test selectors, paths, commands, and code unchanged."
            )
            if task.test_strategy:
                prompt += f"\nPlanning test strategy: {task.test_strategy}"
            if task.scope_paths:
                prompt += f"\nApproved Task scope: {json.dumps(task.scope_paths)}"
            from harness.goal.skills import assigned_skill_context, normalize_goal_skills

            test_skills = normalize_goal_skills(task.skill_names)
            test_skill_context = assigned_skill_context(test_skills)
            if test_skill_context:
                prompt += (
                    f"\n\nAssigned workflow skills: {', '.join(test_skills)}\n"
                    f"{test_skill_context}\n"
                    "These skills describe method only; the test-only write boundary and JSON response contract win."
                )
            if impact_context:
                prompt += (
                    "\n\nCross-Task impact context:\n"
                    f"{json.dumps(impact_context[-8:], ensure_ascii=False)[:5000]}\n"
                    "Add focused coverage for these interactions. Do not only repeat task-local tests."
                )
            # A round limit is a scheduling slice, not a project-size limit.
            # The shared supervisor extends only when a test artifact changes.
            writer_deadline = self._deadline(state)
            def invoke_writer(active_prompt: str, description: str, _slice: int, stats: AgentTaskStats) -> str:
                self._phase_in_flight = True
                try:
                    clear_goal_permission_flags()
                    set_goal_noninteractive(True)
                    return run_agent_task(
                        description=description,
                        prompt=active_prompt,
                        agent_type="goal_test_writer",
                        cwd=str(root),
                        max_rounds=TEST_WRITER_MAX_ROUNDS,
                        cancel_check=self._interrupted,
                        deadline=writer_deadline,
                        stats=stats,
                        write_roots=write_roots,
                    )
                finally:
                    set_goal_noninteractive(False)
                    self._phase_in_flight = False

            def test_progress(previous: dict[str, bytes], current: dict[str, bytes], stats: AgentTaskStats) -> StageProgress:
                created = sorted(set(current) - set(previous))
                changed = sorted(path for path in set(current) & set(previous) if current[path] != previous[path])
                advanced = bool(created)
                if changed and not created:
                    summary = "only existing test files changed; awaiting a focused new test artifact"
                elif created:
                    summary = f"created focused test artifact: {', '.join(created[:4])}"
                else:
                    summary = "no new test artifact in this slice"
                return StageProgress(
                    advanced=advanced,
                    summary=summary,
                    checkpoint={
                        "created_test_files": created[:12],
                        "changed_existing_test_files": changed[:12],
                        "write_paths": stats.write_paths[-12:],
                        "tool_errors": stats.tool_errors[-4:],
                    },
                )

            supervised = StageSupervisor(StagePolicy(
                name="test_generation",
                slice_rounds=TEST_WRITER_MAX_ROUNDS,
                max_idle_slices=TEST_WRITER_MAX_IDLE_CHUNKS,
            )).run(
                invoke=invoke_writer,
                initial_prompt=prompt,
                initial_description=f"generate tests for task {task.id}",
                continuation_prompt=lambda _slice, progress, _idle: prompt + (
                    "\n\nContinue from the current workspace state. The prior slice reached its tool-round "
                    f"budget. Supervisor evidence: {progress.summary}. Inspect existing new tests, then continue "
                    "the Task. Do not modify existing test files."
                ),
                continuation_description=lambda slice_number: f"continue generating tests for task {task.id} (slice {slice_number})",
                snapshot=lambda: self._snapshot_test_tree(root, write_roots),
                assess_progress=test_progress,
                on_slice=lambda item: setattr(state, "total_llm_rounds", state.total_llm_rounds + item.stats.llm_rounds),
            )
            stats = supervised.stats
            raw = supervised.raw
            writer_stalled = supervised.stalled
            if self._honor_control_request(state):
                return

            if stats.stop_reason == "deadline":
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(
                    state,
                    "test_generation_timeout",
                    stop_reason=StopReason.test_generation_timeout.value,
                )
                state.last_error = f"Test generation reached the operation timeout for Task {task.id}."
                save_goal(state)
                return
            empty_response = stats.stop_reason == "empty_response"
            if (
                stats.stop_reason in {"provider_error", "configuration_error"}
                or str(raw).startswith("Error:")
                or (str(raw).startswith("[goal_test_writer] failed:") and not empty_response)
                or str(raw).startswith("[goal_test_writer] stopped:")
            ):
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                state.last_error = f"Goal test writer unavailable: {str(raw)[:3_000]}"
                self._pause(
                    state,
                    "goal_test_writer_unavailable",
                    stop_reason=StopReason.provider_unavailable.value,
                )
                save_goal(state)
                return
            if goal_permission_pending():
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(
                    state,
                    "goal_test_generation_permission_required",
                    stop_reason=StopReason.permission_wait.value,
                )
                state.last_error = "test generation needs permission outside the approved Goal boundary"
                save_goal(state)
                return
            after_catalog = (
                collect_pytest_catalog(root)
                if adapter.id == "pytest"
                else adapter.discover(verification_context)
            )
            if empty_response or writer_stalled:
                # A provider can complete tool calls but omit its final text.
                # Preserve a newly written test long enough to ask for its
                # required selector mapping without giving the retry any write
                # tools. If no new collected selector exists, there is no
                # durable test artifact that can safely advance this Task.
                generated_selectors = tuple(
                    selector for selector in after_catalog.selectors
                    if selector not in before_catalog.selectors
                )
                if generated_selectors:
                    completion_prompt = (
                        "You already created focused tests for this Task but did not submit the required final JSON. "
                        "Do not call tools or edit files. Reply ONLY with one JSON object mapping every acceptance case "
                        "to the collected selectors below.\n\n"
                        f"Task: {task.subject}\n"
                        f"Acceptance cases: {json.dumps(task.acceptance_cases, ensure_ascii=False)}\n"
                        f"New collected selectors: {json.dumps(generated_selectors)}\n"
                        '{"test_selectors":["exact selector"],"case_selectors":{"AC1":["exact selector"]}}'
                    )
                    retry_stats = AgentTaskStats()
                    self._phase_in_flight = True
                    try:
                        set_goal_noninteractive(True)
                        raw = run_agent_task(
                            description=f"submit generated test selectors for task {task.id}",
                            prompt=completion_prompt,
                            agent_type="goal_test_writer",
                            cwd=str(root),
                            max_rounds=1,
                            cancel_check=self._interrupted,
                            deadline=self._deadline(state),
                            stats=retry_stats,
                            tools_override=(),
                        )
                    finally:
                        set_goal_noninteractive(False)
                        self._phase_in_flight = False
                    state.total_llm_rounds += retry_stats.llm_rounds
                    if self._honor_control_request(state):
                        return
                    empty_response = retry_stats.stop_reason == "empty_response"
                if empty_response or (writer_stalled and not generated_selectors):
                    self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                    stop_reason = (
                        StopReason.test_writer_stalled.value
                        if writer_stalled
                        else StopReason.test_writer_empty_response.value
                    )
                    detail = (
                        f"Test writer reached {TEST_WRITER_MAX_IDLE_CHUNKS} consecutive round slices without "
                        f"creating or changing a test artifact for Task {task.id}."
                        if writer_stalled
                        else (
                            f"Test writer used tools but did not submit a final result for Task {task.id}; "
                            "no collected new test selector was available to bind."
                        )
                    )
                    self._pause(
                        state,
                        "goal_test_writer_stalled" if writer_stalled else "goal_test_writer_empty_response",
                        stop_reason=stop_reason,
                    )
                    state.last_error = detail
                    save_goal(state)
                    return
            selectors = self._selectors_from_generation(raw, root, catalog=after_catalog)
            case_selectors = self._case_selectors_from_generation(raw, selectors, task.acceptance_cases)
            if not selectors:
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_required", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} needs a collected pytest selector before execution."
                save_goal(state)
                return
            required_cases = {
                str(case.get("id")) for case in task.acceptance_cases
                if isinstance(case, dict) and case.get("id")
            }
            if not required_cases.issubset(case_selectors):
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_case_mapping_required", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test writer must map every acceptance case to collected selectors."
                save_goal(state)
                return
            if any(selector in before_catalog.selectors for selector in selectors):
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_reused_existing_selector", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test writer reused an existing selector; it must add focused coverage."
                save_goal(state)
                return
            # The writer is allowed to add a focused test, never alter any
            # pre-existing fixture/helper/conftest under a permitted test root.
            # Hashing only collected nodes misses these semantic escape hatches.
            after_tree = self._snapshot_test_tree(root, write_roots)
            changed_existing = [
                path for path, contents in before_tree.items()
                if after_tree.get(path) != contents
            ]
            if changed_existing:
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_changed_existing_test", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test writer modified existing test files: {', '.join(changed_existing)}"
                save_goal(state)
                return
            command = adapter.build_command(selectors)
            baseline = run_verification(
                command,
                workspace=root,
                timeout_s=state.operation_timeout_seconds,
                cancel_check=self._interrupted,
            )
            if self._honor_control_request(state):
                return
            output = str(getattr(baseline, "stdout", "") or "").lower()
            infrastructure_failure = "error collecting" in output or "importerror" in output or "fixture" in output and "error" in output
            posthoc = bool(task.verification_spec.get("allow_posthoc_test"))
            if baseline.error or baseline.timed_out or (baseline.passed and not posthoc) or infrastructure_failure:
                detail = baseline.error or (
                    "generated tests passed before implementation; they do not prove the missing behavior"
                    if baseline.passed
                    else "generated test baseline failed outside the requested behavior"
                )
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_baseline_failed", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test baseline is invalid: {detail}"
                save_goal(state)
                return
            previous_selectors = tuple(str(item) for item in task.verification_spec.get("previous_selectors", []) if item)
            all_selectors = tuple(dict.fromkeys((*previous_selectors, *selectors)))
            files = tuple(dict.fromkeys(item.split("::", 1)[0] for item in all_selectors))
            baseline_evidence = evidence_from_result(
                baseline,
                workspace=str(root),
                selectors=selectors,
                collected_count=len(selectors),
                verified_by="goal_test_baseline",
            ).to_dict()
            bound_spec = VerificationSpec(
                adapter=adapter.id,
                command=adapter.build_command(all_selectors),
                test_files=files,
                selectors=all_selectors,
                source="generated",
                collected_count=len(selectors),
                baseline_result="posthoc_passing" if baseline.passed else "failing",
                confidence="high",
                baseline_evidence=baseline_evidence,
                test_hashes=self._test_file_hashes(root, files),
                covers=tuple(case_id for case_id in case_selectors),
                case_selectors=case_selectors,
                owners=tuple(
                    dict.fromkeys(
                        str(owner)
                        for owner in (task.verification_spec.get("owners") or [task.id])
                        if owner
                    )
                ),
            ).to_dict()
            # Binding replaces the verification spec. Preserve the durable
            # cross-Task reason that caused this additive test generation.
            if impact_context:
                bound_spec["impact_context"] = impact_context
            bound_spec["generated_files"] = [
                {
                    "path": path,
                    "sha256": bound_spec["test_hashes"].get(path),
                    "created": path not in before_tree,
                }
                for path in files
            ]
            bound = bind_task_verification(task.id, bound_spec)
            record_test_binding(
                state,
                bound,
                bound.verification_spec,
                kind="integration" if len(bound.verification_spec.get("owners") or []) > 1 else "task",
            )
        if not state.execution_approved:
            # A previous test-generation retry may have left a transient
            # diagnostic behind. Once bindings are ready, status must reflect
            # the real user-approval pause rather than that stale failure.
            state.last_error = None
            self._pause(
                state,
                "tests_ready_for_user_approval",
                stop_reason=StopReason.user_approval_required.value,
            )
            return
        self._apply(
            state,
            GoalPhase.SELECT_TASK,
            "task_tests_deferred_for_dependencies" if deferred else "task_tests_bound",
        )

    @staticmethod
    def _selectors_from_generation(raw: str, workspace: Path, *, catalog=None) -> tuple[str, ...]:
        try:
            data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            requested = tuple(str(item) for item in data.get("test_selectors", []))
        except (ValueError, TypeError, json.JSONDecodeError):
            return ()
        catalog = catalog or collect_pytest_catalog(workspace)
        if not requested:
            return ()
        resolved: list[str] = []
        for item in requested:
            candidate = item.replace("\\", "/")
            if catalog.contains(candidate):
                resolved.append(candidate)
                continue
            # Test writers may return a new test file instead of every node.
            # Expand only nodes pytest actually collected, including params.
            if candidate in catalog.test_files:
                resolved.extend(
                    selector for selector in catalog.selectors
                    if selector.split("::", 1)[0] == candidate
                )
                continue
            # A writer can name a parameterized test function without its
            # generated ``[case]`` suffix. Bind every concrete node from the
            # collection rather than accepting the uncollected shorthand.
            parameterized = [
                selector for selector in catalog.selectors
                if selector.startswith(f"{candidate}[")
            ]
            if parameterized:
                resolved.extend(parameterized)
                continue
            return ()
        return tuple(dict.fromkeys(resolved))

    @staticmethod
    def _case_selectors_from_generation(raw: str, selectors: tuple[str, ...], cases) -> dict[str, list[str]]:
        """Accept only explicit case->selector claims from a test writer."""
        try:
            data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}
        mapping = data.get("case_selectors") if isinstance(data, dict) else None
        if not isinstance(mapping, dict):
            return {}
        valid = set(selectors)
        case_ids = {str(item.get("id")) for item in cases if isinstance(item, dict) and item.get("id")}
        result: dict[str, list[str]] = {}
        for case_id, values in mapping.items():
            if str(case_id) not in case_ids or not isinstance(values, list):
                continue
            chosen = [str(value) for value in values if str(value) in valid]
            if chosen:
                result[str(case_id)] = list(dict.fromkeys(chosen))
        return result

    @staticmethod
    def _test_file_hashes(root: Path, paths) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for raw in paths:
            rel = str(raw).replace("\\", "/")
            try:
                hashes[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
            except OSError:
                continue
        return hashes

    @staticmethod
    def _snapshot_test_tree(root: Path, roots: tuple[str, ...]) -> dict[str, bytes]:
        """Capture only the files a test-generation worker may mutate."""
        snapshot: dict[str, bytes] = {}
        workspace = root.resolve()
        for rel_root in roots:
            try:
                directory = (workspace / rel_root).resolve()
            except OSError:
                continue
            if not directory.is_relative_to(workspace):
                continue
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    snapshot[path.relative_to(workspace).as_posix()] = path.read_bytes()
                except OSError:
                    continue
        return snapshot

    @staticmethod
    def _restore_test_tree(
        root: Path,
        snapshot: dict[str, bytes],
        allowed_roots: tuple[str, ...],
        *,
        expected_after: dict[str, bytes] | None = None,
    ) -> None:
        """Undo an invalid generation attempt without touching production code.

        ``expected_after`` lets the caller avoid overwriting a file that was
        edited by someone else after the generation attempt finished.
        """
        workspace = root.resolve()
        roots: list[Path] = []
        for rel_root in allowed_roots:
            try:
                directory = (workspace / rel_root).resolve()
            except OSError:
                continue
            if directory.is_relative_to(workspace):
                roots.append(directory)
        for directory in sorted(set(roots), key=lambda item: len(item.parts), reverse=True):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*"), reverse=True):
                if not path.is_file():
                    continue
                rel = path.relative_to(workspace).as_posix()
                if rel not in snapshot:
                    try:
                        if expected_after is not None and expected_after.get(rel) != path.read_bytes():
                            continue
                        path.unlink()
                    except OSError:
                        pass
        for rel, contents in snapshot.items():
            try:
                path = (workspace / rel).resolve()
            except OSError:
                continue
            if not path.is_relative_to(workspace) or not any(path.is_relative_to(item) for item in roots):
                continue
            try:
                current = path.read_bytes() if path.exists() else None
                if expected_after is not None and expected_after.get(rel) != current:
                    continue
                if current != contents:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(contents)
            except OSError:
                continue

    @staticmethod
    def _test_write_roots(catalog) -> tuple[str, ...]:
        roots = {"tests", "test", "__tests__"}
        for test_file in getattr(catalog, "test_files", ()):
            parts = Path(test_file).parts[:-1]
            for index, part in enumerate(parts):
                if part.lower() in {"tests", "test", "__tests__"}:
                    roots.add(Path(*parts[:index + 1]).as_posix())
                    break
        return tuple(sorted(roots))

    def _select_task(self, state: GoalState) -> None:
        from harness.tasks import load_task

        if not state.execution_approved:
            self._pause(
                state,
                "execution_approval_required",
                stop_reason=StopReason.user_approval_required.value,
            )
            return

        for task_id in state.task_ids:
            task = load_task(task_id)
            if task.status == "completed":
                continue
            if not all(load_task(dep).status == "completed" for dep in task.blockedBy):
                continue
            is_new_task = state.current_task_id != task_id
            state.current_task_id = task_id
            if task.verification_state == "needs_generation":
                save_goal(state)
                self._apply(state, GoalPhase.PREPARE_TESTS, "task_test_generation_required")
                return
            if is_new_task:
                state.attempts = 0
                state.no_progress_count = 0
            save_goal(state)
            self._apply(state, GoalPhase.ACT if task.status == "in_progress" else GoalPhase.CLAIM, "task_selected")
            return
        self._apply(state, GoalPhase.FULL_VERIFY, "all_tasks_completed")

    def _claim(self, state: GoalState) -> None:
        from harness.tasks import claim_task, load_task, record_task_start, save_task
        from harness.verification.snapshot import capture_code_snapshot, capture_dirty_file_hashes

        task = load_task(state.current_task_id)
        spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
        if spec.get("source") == "discovered" and not spec.get("test_hashes"):
            spec["test_hashes"] = self._test_file_hashes(
                Path(_execution_workspace(state)), spec.get("test_files") or []
            )
            task.verification_spec = spec
            save_task(task)
        claimed = claim_task(state.current_task_id, owner=f"goal:{state.id}")
        if not claimed.startswith("Claimed"):
            state.last_error = claimed
            self._pause(state, "task_claim_conflict", stop_reason=StopReason.workspace_changed.value)
            return
        record_task_start(
            state.current_task_id,
            snapshot=capture_code_snapshot(_execution_workspace(state)),
            diff=self._workspace_diff(_execution_workspace(state)),
            dirty_hashes=capture_dirty_file_hashes(_execution_workspace(state)),
        )
        self._apply(state, GoalPhase.ACT, "task_claimed")

    def _act(self, state: GoalState) -> None:
        from harness.goal.memory import write_handoff
        from harness.goal.prompt import build_goal_act_prompt
        from harness.tasks import load_task

        task = load_task(state.current_task_id)
        before = self._progress_snapshot(state)
        stats = AgentTaskStats()
        state.worker_generation += 1
        save_goal(state)
        worker_context = self._goal_worker_context()
        prompt = build_goal_act_prompt(
            state,
            task,
            project_instructions=str(worker_context.get("project_instructions") or ""),
            memories=str(worker_context.get("memories") or ""),
        )
        write_handoff(state, task, phase=GoalPhase.ACT.value)
        self._phase_in_flight = True
        try:
            from harness.agents.registry import get_agent_profile, validate_agent_model

            goal_worker = get_agent_profile("goal_worker")
            worker_error = validate_agent_model("goal_worker")
            if worker_error or goal_worker is None:
                state.last_error = worker_error or "goal_worker agent profile is unavailable"
                # A stale/misconfigured route is recoverable after the model or
                # provider configuration is corrected.  Treating it as an
                # internal terminal failure made `/goal resume` impossible and
                # hid the actionable route error from the user.
                self._pause(
                    state,
                    "goal_worker_configuration_unavailable",
                    stop_reason=StopReason.provider_unavailable.value,
                )
                save_goal(state)
                return
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            clear_cancel()
            summary = run_agent_task(
                description=f"implement Goal task {task.id}",
                prompt=prompt,
                agent_type="goal_worker",
                cwd=_execution_workspace(state),
                max_rounds=state.worker_round_limit,
                cancel_check=self._interrupted,
                deadline=self._deadline(state),
                stats=stats,
                write_roots=tuple(task.scope_paths) or None,
            )
        finally:
            set_goal_noninteractive(False)
            self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        state.attempts += 1
        scope_error = self._validate_task_scope(state, task)
        if scope_error:
            emit_stage_supervision(
                "blocked",
                stage="implementation",
                slice=state.worker_generation,
                reason=scope_error,
                checkpoint={"task_id": task.id},
            )
            self._pause(state, "task_scope_violation", stop_reason=StopReason.autonomy_blocked.value)
            state.last_error = scope_error
            save_goal(state)
            return
        worker_progressed = self._progress_snapshot(state) != before
        state.no_progress_count = state.no_progress_count + 1 if not worker_progressed else 0
        emit_stage_supervision(
            "slice_finished",
            stage="implementation",
            slice=state.worker_generation,
            stop_reason=stats.stop_reason,
            rounds=stats.llm_rounds,
            tools=stats.tool_count,
            progress=worker_progressed,
            progress_summary=(
                "Task evidence or scoped code snapshot changed"
                if worker_progressed else "no Task evidence or scoped code change in this worker slice"
            ),
            idle_slices=state.no_progress_count,
            checkpoint={"task_id": task.id, "worker_rollovers": state.worker_rollovers},
        )
        write_handoff(state, task, phase=GoalPhase.VERIFY.value, summary=summary)
        if self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
        elif goal_permission_pending():
            # A non-interactive Goal must never auto-approve an ``ask`` tool
            # decision.  This is recoverable after the user adjusts policy or
            # grants approval, so preserve the Task checkpoint as a pause.
            self._pause(
                state,
                "goal_permission_required",
                stop_reason=StopReason.permission_wait.value,
            )
            state.last_error = "a tool required permission outside the approved Goal boundary"
            save_goal(state)
        elif self._pause_event.is_set():
            self._pause(state, "user_pause")
        elif stats.stop_reason == "provider_error":
            state.last_error = summary[:4_000]
            self._pause(
                state,
                "goal_worker_provider_error",
                stop_reason=StopReason.provider_unavailable.value,
            )
        elif stats.stop_reason == "max_rounds":
            state.worker_rollovers += 1
            self._apply(state, GoalPhase.ROLLOVER, "worker_round_limit_reached")
        elif state.no_progress_count >= NO_PROGRESS_REPLAN_LIMIT:
            state.last_error = f"{state.no_progress_count} workers made no observable progress"
            self._apply(state, GoalPhase.REPAIR_PLAN, "worker_no_progress", error=state.last_error)
        else:
            self._apply(state, GoalPhase.VERIFY, "goal_worker_finished")

    def _rollover(self, state: GoalState) -> None:
        """A bounded worker ended; verification decides whether another is needed."""
        self._apply(state, GoalPhase.VERIFY, "worker_handoff_saved")

    def _goal_worker_context(self) -> dict[str, Any]:
        """Keep project rules, never the user's stale chat state or Todo list."""
        allowed = {"project_instructions", "memories", "connected_mcp"}
        return {key: value for key, value in self._context.items() if key in allowed}

    def _verify(self, state: GoalState) -> None:
        task = verify_task_command(
            state.current_task_id,
            workspace=_execution_workspace(state),
            timeout_s=state.operation_timeout_seconds,
            cancel_check=self._interrupted,
        )
        if self._honor_control_request(state):
            return
        if task.verification_state == "passing":
            state.consecutive_failures = 0
            state.last_error = None
            self._apply(state, GoalPhase.EVALUATE if task.evaluation_required else GoalPhase.CLEAN_CHECK, "task_verification_passed")
            return
        state.consecutive_failures += 1
        state.last_error = task.last_error
        self._apply(state, GoalPhase.ACT, "task_verification_failed", error=task.last_error)

    def _evaluate(self, state: GoalState) -> None:
        stats = AgentTaskStats()
        task = run_task_evaluation(
            state.current_task_id,
            _execution_workspace(state),
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if self._honor_control_request(state):
            return
        evaluation = task.evaluation or {}
        if evaluation.get("passed") is None:
            state.last_error = str(
                evaluation.get("error") or "evaluator did not return a valid verdict"
            )
            stop_reason = (
                StopReason.provider_unavailable.value
                if evaluation.get("agent_stop_reason") in {"provider_error", "configuration_error"}
                else StopReason.evaluation_unavailable.value
            )
            self._pause(
                state,
                "evaluation_unavailable",
                stop_reason=stop_reason,
            )
            save_goal(state)
            return
        if evaluation.get("passed") is True:
            self._apply(state, GoalPhase.CLEAN_CHECK, "evaluation_passed")
            return
        state.last_error = str(evaluation.get("error") or evaluation.get("summary") or "evaluator rejected Task")
        self._apply(state, GoalPhase.REPAIR_PLAN, "evaluation_requires_repair", error=state.last_error)

    def _repair_plan(self, state: GoalState) -> None:
        from harness.goal.memory import append_decisions
        from harness.goal.repair import plan_task_repair
        from harness.tasks import load_task, record_task_repair, request_task_test_repair

        task = load_task(state.current_task_id)
        evaluation = task.evaluation or {"passed": False, "summary": state.last_error or "Goal verification failed", "route": "implementation_fix"}
        if len(task.repair_history) >= MAX_REPAIR_ATTEMPTS_PER_TASK:
            detail = (
                f"Task {task.id} reached {MAX_REPAIR_ATTEMPTS_PER_TASK} repair plans without completion; "
                "review its acceptance cases, evidence, and scope before resuming."
            )
            state.last_error = detail
            self._pause(state, "repair_limit_reached", stop_reason=StopReason.repair_limit_reached.value)
            return
        if evaluation.get("passed") is None:
            self._fail(
                state,
                StopReason.evaluation_unavailable,
                str(evaluation.get("error") or "evaluator did not return a valid verdict"),
            )
            return
        stats = AgentTaskStats()
        decision = plan_task_repair(
            state,
            task,
            evaluation,
            cwd=_execution_workspace(state),
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if self._honor_control_request(state):
            return
        record = {"evaluation": evaluation, **decision.to_dict(), "at": time.time()}
        record_task_repair(task.id, record)
        if decision.assumptions:
            append_decisions(state, task, list(decision.assumptions), source="repair_planner")
        if decision.unavailable:
            state.last_error = decision.error or "repair planner unavailable"
            format_error = state.last_error.startswith((
                "repair planner returned no JSON",
                "invalid repair JSON:",
                "repair planner output is not an object",
                "unsupported repair action:",
                "repair action needs instructions",
            ))
            self._pause(
                state,
                "repair_planner_format_error" if format_error else "repair_planner_unavailable",
                stop_reason=(
                    StopReason.repair_plan_format_error.value
                    if format_error
                    else StopReason.provider_unavailable.value
                ),
            )
            return
        if decision.action == "blocked":
            self._fail(state, StopReason.autonomy_blocked, decision.error or decision.summary or "repair planner blocked")
            return
        state.repair_attempts += 1
        state.last_error = decision.summary or None
        save_goal(state)
        if decision.action in {"test_gap", "replan"}:
            request_task_test_repair(task.id)
            self._apply(
                state,
                GoalPhase.PREPARE_TESTS,
                "repair_replanned_task_tests" if decision.action == "replan" else "repair_requires_test_coverage",
            )
            return
        self._apply(
            state,
            GoalPhase.SELECT_TASK if task.status == "pending" else GoalPhase.ACT,
            "repair_plan_ready",
        )

    def _clean_check(self, state: GoalState) -> None:
        from harness.tasks import complete_task, load_task, save_task

        result = complete_task(state.current_task_id, clean_check_mode="enforce")
        if result.startswith("Completed") or "already completed" in result:
            self._apply(state, GoalPhase.IMPACT_REVIEW, "task_completed")
            return
        state.consecutive_failures += 1
        # The next worker is fresh. Persist the clean-gate failure on the
        # Task so it has the same concrete evidence as a normal test failure.
        task = load_task(state.current_task_id)
        task.last_error = result
        save_task(task)
        self._apply(state, GoalPhase.ACT, "clean_check_failed", error=result)

    def _impact_review(self, state: GoalState) -> None:
        from harness.goal.impact import review_test_impact
        from harness.goal.memory import append_decisions
        from harness.tasks import load_task, request_task_test_repair, save_task

        completed = load_task(state.current_task_id)
        pending = []
        for task_id in state.task_ids:
            if task_id == completed.id:
                continue
            candidate = load_task(task_id)
            if candidate.status == "pending":
                pending.append(candidate)
        stats = AgentTaskStats()
        decision = review_test_impact(
            state,
            completed,
            pending,
            cwd=_execution_workspace(state),
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if self._honor_control_request(state):
            return
        if decision.unavailable:
            state.last_error = decision.reason or "test impact reviewer unavailable"
            self._pause(
                state,
                "test_impact_review_unavailable",
                stop_reason=StopReason.provider_unavailable.value,
            )
            return
        if decision.format_error:
            state.last_error = decision.reason or "test impact reviewer returned invalid JSON"
            self._pause(
                state,
                "test_impact_review_format_error",
                stop_reason=StopReason.impact_review_format_error.value,
            )
            return
        if decision.action == "add_tests" and decision.task_id:
            target = request_task_test_repair(decision.task_id)
            target.verification_spec["owners"] = list(dict.fromkeys((completed.id, target.id)))
            existing_context = target.verification_spec.get("impact_context") or []
            if not isinstance(existing_context, list):
                existing_context = []
            context = [entry for entry in existing_context if isinstance(entry, dict)]
            context = [entry for entry in context if entry.get("source_task_id") != completed.id]
            context.append(
                {
                    "source_task_id": completed.id,
                    "source_task_subject": str(completed.subject)[:400],
                    "reason": (decision.reason or f"impact from {completed.id}")[:1200],
                    "required_coverage": (
                        "Add focused interaction coverage for this upstream Task and the target Task "
                        "before implementation; do not only repeat task-local tests."
                    ),
                }
            )
            target.verification_spec["impact_context"] = context[-8:]
            save_task(target)
            append_decisions(
                state,
                target,
                [{"decision": "Add cross-Task coverage", "basis": decision.reason or f"impact from {completed.id}"}],
                source="test_impact_review",
            )
        self._apply(state, GoalPhase.SELECT_TASK, "test_impact_reviewed")

    def _full_verify(self, state: GoalState) -> None:
        for task_id in state.task_ids:
            task = reverify_task_command(
                task_id,
                workspace=_execution_workspace(state),
                timeout_s=state.operation_timeout_seconds,
                cancel_check=self._interrupted,
            )
            if self._honor_control_request(state):
                return
            if task.verification_state != "passing":
                from harness.tasks import reopen_task_for_goal_repair

                detail = f"Task {task_id} final binding failed: {task.last_error}"
                self._record_final_verification(
                    state,
                    status="blocked",
                    error=detail,
                )
                reopen_task_for_goal_repair(task_id, error=detail)
                state.current_task_id = task_id
                state.last_error = detail
                self._apply(state, GoalPhase.REPAIR_PLAN, "final_task_binding_requires_repair", error=detail)
                return
        state.final_verification = {
            "status": "running",
            "command": state.verification,
            "updated_at": time.time(),
        }
        save_goal(state)
        _emit_goal("goal_status", state)
        result = run_verification(
            state.verification,
            workspace=Path(_execution_workspace(state)),
            timeout_s=state.operation_timeout_seconds,
            cancel_check=self._interrupted,
        )
        if self._honor_control_request(state):
            return
        if self._final_verification_was_interrupted(result, state.verification):
            self._record_final_verification(state, status="interrupted", result=result)
            detail = "Full verification was interrupted before completion; retry it with /goal resume."
            state.last_error = detail
            save_goal(state)
            self._pause(
                state,
                "full_verification_interrupted",
                stop_reason=StopReason.full_verification_interrupted.value,
            )
            return

        self._record_final_verification(state, status="passed" if result.passed else "failed", result=result)
        if result.passed:
            self._apply(state, GoalPhase.DONE, "goal_verification_passed")
        elif getattr(result, "error", None) or getattr(result, "timed_out", False):
            detail = getattr(result, "error", None) or "full verification timed out"
            state.last_error = detail
            self._pause(
                state,
                "full_verification_unavailable",
                stop_reason=StopReason.full_verification_failed.value,
            )
        else:
            self._queue_goal_repair(
                state,
                result.error or f"full verification failed with exit code {result.exit_code}",
            )

    @staticmethod
    def _final_verification_was_interrupted(result: Any, command: str = "") -> bool:
        """Recognize pytest's interrupted result separately from a test failure."""
        output = "\n".join(
            str(getattr(result, field, "") or "")
            for field in ("stdout", "stderr", "error")
        ).lower()
        effective_command = str(getattr(result, "command", "") or command).lower()
        is_pytest = "pytest" in effective_command
        return is_pytest and (
            getattr(result, "exit_code", None) == 2 or "keyboardinterrupt" in output
        )

    def _queue_goal_repair(self, state: GoalState, detail: str) -> None:
        """Analyze final-test evidence before reopening or adding a Task."""
        from harness.goal.memory import append_decisions
        from harness.goal.repair import plan_goal_regression_repair
        from harness.tasks import (
            create_task,
            load_task,
            record_task_evaluation,
            record_task_repair,
            reopen_task_for_goal_repair,
        )

        try:
            tasks = [load_task(task_id) for task_id in state.task_ids]
        except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
            state.last_error = f"cannot analyze final verification failure: {exc}"
            self._pause(state, "full_verification_tasks_unavailable", stop_reason=StopReason.full_verification_failed.value)
            return
        stats = AgentTaskStats()
        decision = plan_goal_regression_repair(
            state,
            tasks,
            cwd=_execution_workspace(state),
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if self._honor_control_request(state):
            return
        append_decisions(
            state,
            None,
            [{
                "decision": f"Full verification analysis: {decision.action}",
                "basis": decision.summary or decision.instructions or decision.error or detail,
            }],
            source="final_verification_analysis",
        )
        if decision.unavailable or decision.action == "pause":
            state.last_error = decision.error or decision.summary or detail
            self._pause(
                state,
                "full_verification_analysis_paused",
                stop_reason=(
                    StopReason.provider_unavailable.value
                    if decision.unavailable
                    else StopReason.full_verification_failed.value
                ),
            )
            return

        final = state.final_verification if isinstance(state.final_verification, dict) else {}
        selector = self._failed_pytest_selector(str(final.get("stdout_tail") or ""))
        if decision.action == "reopen_existing":
            task = reopen_task_for_goal_repair(
                str(decision.task_id),
                error=f"Final verification failed at {selector or 'an unparsed selector'}: {detail}",
            )
            record_task_repair(task.id, {
                "source": "final_verification_analysis",
                **decision.to_dict(),
                "final_verification": final,
                "at": time.time(),
            })
            state.current_task_id = task.id
            state.last_error = detail
            self._apply(state, GoalPhase.REPAIR_PLAN, "final_regression_reopened_owner_task", error=detail)
            return

        # A new Task is valid only after the model explicitly chose it and a
        # concrete failing pytest selector gives it a machine-verifiable gate.
        if selector is None:
            state.last_error = f"{detail}. The planner requested a repair Task but no failing pytest selector was recorded."
            self._pause(state, "full_verification_repair_unbound", stop_reason=StopReason.full_verification_failed.value)
            return
        fingerprint = hashlib.sha256(
            json.dumps(final, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
        ).hexdigest()
        for existing in tasks:
            spec = existing.verification_spec if isinstance(existing.verification_spec, dict) else {}
            if spec.get("goal_regression_fingerprint") == fingerprint:
                state.last_error = "This full verification failure already has a repair Task; refusing to create a duplicate."
                self._pause(state, "full_verification_repair_duplicate", stop_reason=StopReason.full_verification_failed.value)
                return
        root = Path(_execution_workspace(state))
        catalog = collect_pytest_catalog(root)
        if not catalog.contains(selector):
            state.last_error = f"Failed selector {selector!r} is no longer collected; repair Task was not created."
            self._pause(state, "full_verification_repair_selector_unavailable", stop_reason=StopReason.full_verification_failed.value)
            return
        test_file = selector.split("::", 1)[0]
        name = f"goal regression repair {state.repair_attempts + 1}"
        verification_spec = {
            "adapter": "pytest",
            "command": build_pytest_command((selector,)),
            "test_files": [test_file],
            "selectors": [selector],
            "source": "discovered",
            "collected_count": 1,
            "baseline_result": "failing",
            "confidence": "high",
            "test_hashes": self._test_file_hashes(root, (test_file,)),
            "covers": ["GOAL_REGRESSION"],
            "owners": [],
            "goal_regression_fingerprint": fingerprint,
            "final_verification": final,
        }
        task = create_task(
            name,
            f"Repair {selector}: {decision.instructions}",
            goal_id=state.id,
            acceptance_cases=[{
                "id": "GOAL_REGRESSION",
                "given": "the reported full verification failure",
                "when": selector,
                "then": "the selector and full verification command exit with code 0",
            }],
            skill_names=["systematic-debugging"],
            verification_spec=verification_spec,
            evaluation_required=True,
        )
        record_task_evaluation(task.id, {
            "passed": False,
            "route": "implementation_fix",
            "summary": decision.summary or detail,
            "findings": [{"issue": detail, "severity": "high", "evidence": "full verification"}],
        })
        record_task_repair(task.id, {
            "source": "final_verification_analysis",
            **decision.to_dict(),
            "final_verification": final,
            "at": time.time(),
        })
        state.task_ids.append(task.id)
        state.task_name_ids[name] = task.id
        state.task_plan.append({
            "name": name,
            "behavior": task.description,
            "depends_on": [],
            "acceptance_cases": task.acceptance_cases,
            "skills": task.skill_names,
            "verification_spec": task.verification_spec,
        })
        state.current_task_id = task.id
        state.last_error = detail
        self._apply(state, GoalPhase.REPAIR_PLAN, "final_regression_analysis_created_repair_task", error=detail)

    @staticmethod
    def _failed_pytest_selector(output: str) -> str | None:
        for line in output.splitlines():
            match = re.match(r"^FAILED\s+([^\s]+\.py::[^\s]+)", line.strip())
            if match:
                return match.group(1).replace("\\", "/")
        return None

    def _record_final_verification(self, state: GoalState, *, status: str, result=None, error: str | None = None) -> None:
        """Persist the machine result of the whole-goal regression gate."""
        record: dict[str, Any] = {
            "status": status,
            "command": state.verification,
            "updated_at": time.time(),
        }
        if result is not None:
            evidence = evidence_from_result(
                result,
                workspace=_execution_workspace(state),
                verified_by="goal_final",
            )
            record.update(evidence.to_dict())
        if error:
            record["error"] = error
        state.final_verification = record
        save_goal(state)
        _emit_goal("goal_status", state)

    @staticmethod
    def _workspace_diff(workspace: str) -> str:
        """Bounded pre-Task diff retained for evaluator and repair context."""
        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout[:30_000] if proc.returncode == 0 else ""

    def _progress_snapshot(self, state: GoalState) -> tuple:
        from harness.tasks import load_task
        from harness.verification.snapshot import capture_code_snapshot

        task = load_task(state.current_task_id)
        return (task.evidence, task.last_error, capture_code_snapshot(_execution_workspace(state)))

    @staticmethod
    def _validate_task_scope(state: GoalState, task) -> str | None:
        """Reject production changes outside the planner's declared scope.

        Files already dirty when the Task was claimed are exempt; they belong
        to the user's pre-existing work and are recorded in the Task baseline.
        """
        from harness.verification.snapshot import capture_dirty_file_hashes

        scope = {str(path).replace("\\", "/").lstrip("./") for path in (task.scope_paths or []) if path}
        if not scope:
            return None
        current = capture_dirty_file_hashes(_execution_workspace(state))
        baseline = {str(path): str(digest) for path, digest in (task.start_dirty_hashes or {}).items()}
        # A file that was already dirty is not exempt if this worker changed it
        # again. Compare digests rather than merely comparing path names.
        changed = {path for path, digest in current.items() if baseline.get(path) != str(digest)}
        outside = sorted(path for path in changed if path not in scope and not any(path.startswith(item.rstrip("/") + "/") for item in scope))
        if outside:
            return "worker changed files outside Task scope: " + ", ".join(outside[:12])
        return None

    def _interrupted(self) -> bool:
        return self._cancel_event.is_set() or self._pause_event.is_set()

    def _honor_control_request(self, state: GoalState) -> bool:
        """Apply durable user controls before interpreting a blocked result."""
        if self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
            return True
        if self._pause_event.is_set():
            self._pause(state, "user_pause")
            return True
        return False

    def _deadline(self, state: GoalState) -> float:
        """Deadline for one model operation, never the lifetime of a Goal."""
        return time.monotonic() + state.operation_timeout_seconds

    def _apply(self, state: GoalState, target: GoalPhase, reason: str, *, error: str | None = None, stop_reason: str | None = None) -> None:
        self.engine.transition(state, target, reason, error=error, stop_reason=stop_reason)
        save_goal(state)
        _emit_goal("goal_phase", state)

    def _pause(self, state: GoalState, reason: str, *, stop_reason: str | None = None) -> None:
        self._apply(state, GoalPhase.PAUSED, reason, stop_reason=stop_reason)

    def _cancel(self, state: GoalState, detail: str) -> None:
        state.last_error = detail
        self._apply(state, GoalPhase.CANCELLED, StopReason.cancelled_by_user.value, error=detail, stop_reason=StopReason.cancelled_by_user.value)

    def _fail(self, state: GoalState, reason: StopReason | str, detail: str) -> None:
        state.last_error = detail
        value = reason.value if isinstance(reason, StopReason) else str(reason)
        self._apply(state, GoalPhase.FAILED, value, error=detail, stop_reason=value)
