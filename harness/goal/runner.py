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
from harness.change_session import ChangeSession, ChangeSessionError
from harness.agents.runner import AgentTaskConversation, AgentTaskStats, run_agent_task
from harness.evaluation import run_task_evaluation
from harness.goal.engine import GoalEngine
from harness.goal.authority import goal_authority
from harness.goal.coordinator import ParallelGoalSupervisor, SupervisorRun
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
    archive_goal_change_patch,
    archive_unsupported_goal,
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
MAX_PERMISSION_BOUNDARY_RETRIES = 3
# Routine progress reports used to start an independent Sol request after
# nearly every worker slice and phase transition.  They competed with the
# implementation worker for the same provider connection while carrying a
# large Goal snapshot.  Supervision remains synchronous at authority and
# failure boundaries, where its decision can actually change execution.
ROUTINE_SUPERVISION_EVENTS: frozenset[str] = frozenset()


def _execution_workspace(state: GoalState) -> str:
    """Return the scoped project root used for code and test operations."""
    root = Path(state.workspace).expanduser().resolve()
    try:
        candidate = Path(state.execution_workspace or root).expanduser().resolve()
    except OSError:
        if state.change_mode == "worktree":
            raise ChangeSessionError("isolated Goal execution workspace is invalid")
        return str(root)
    if state.change_mode == "worktree":
        try:
            worktree = Path(state.change_worktree).expanduser().resolve()
            expected = (worktree / (state.change_execution_relpath or ".")).resolve()
        except OSError as exc:
            raise ChangeSessionError("isolated Goal worktree is invalid") from exc
        if not worktree.is_dir() or not candidate.is_dir() or candidate != expected:
            raise ChangeSessionError("isolated Goal worktree is missing")
        return str(candidate)
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


def goal_permission_requests() -> tuple[dict[str, Any], ...]:
    requests = getattr(_local, "permission_requests", ())
    return tuple(dict(item) for item in requests if isinstance(item, dict))


def mark_goal_permission_pending(request: dict[str, Any] | None = None) -> None:
    _local.permission_pending = True
    if request:
        pending = list(getattr(_local, "permission_requests", ()))
        pending.append(dict(request))
        _local.permission_requests = pending[-12:]


def clear_goal_permission_flags() -> None:
    _local.permission_pending = False
    _local.permission_requests = []
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
                "primary_write": list(task.primary_write) if isinstance(task.primary_write, list) else [],
                "planned_new": list(task.planned_new) if isinstance(task.planned_new, list) else [],
                "conditional_write": list(task.conditional_write) if isinstance(task.conditional_write, list) else [],
                "read_envelope": list(task.read_envelope) if isinstance(task.read_envelope, list) else [],
                "forbidden": list(task.forbidden) if isinstance(task.forbidden, list) else [],
                "evidence_refs": list(task.evidence_refs) if isinstance(task.evidence_refs, list) else [],
                "test_strategy": str(task.test_strategy or ""),
                "verification_spec": task.verification_spec if isinstance(task.verification_spec, dict) else {},
                "evidence_count": len(evidence_items),
                "latest_evidence": evidence_summary,
                "last_error": task.last_error,
            }
        )
    payload = {
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
        "change_mode": state.change_mode,
        "change_merge_state": state.change_merge_state,
        "change_archive_path": state.change_archive_path,
        "final_verification": dict(state.final_verification) if isinstance(state.final_verification, dict) else None,
        "goal_contract": dict(state.goal_contract) if isinstance(state.goal_contract, dict) else {},
        "planning_review": dict(state.planning_review) if isinstance(state.planning_review, dict) else {},
        "execution_preflight": dict(state.execution_preflight) if isinstance(state.execution_preflight, dict) else {},
        "execution_trace": list(state.execution_trace[-24:]),
        "last_error": state.last_error,
        "tasks": tasks,
    }
    if isinstance(state.supervision, dict) and state.supervision:
        payload["supervision"] = dict(state.supervision)
    return payload


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
    if (
        state.status in {GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}
        and state.change_mode == "worktree"
        and state.change_worktree
    ):
        # A process can die after the terminal transition reaches goal.json
        # but before the runner's finally block reclaims the checkout.
        _finalize_terminal_change_session(state)
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
    if state.change_mode == "worktree":
        merge_state = state.change_merge_state or "executing"
        lines.append(f"  Isolation: worktree ({merge_state})")
        if state.change_archive_path:
            lines.append(f"  Change archive: {state.change_archive_path}")
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


def _create_goal_change_session(workspace_root: Path, execution_workspace: Path, goal_id: str) -> tuple[ChangeSession | None, Path]:
    """Create an isolated Goal checkout and map a nested project into it."""
    try:
        session = ChangeSession.create(
            workspace_root,
            execution_workspace=execution_workspace,
            session_id=goal_id,
        )
    except ChangeSessionError as exc:
        message = str(exc).lower()
        if "not a git repository" in message or "no head commit" in message:
            return None, execution_workspace
        raise ValueError(f"Goal isolation unavailable: {exc}") from exc
    return session, session.execution_path()


def _finalize_terminal_change_session(state: GoalState) -> None:
    """Archive failed/cancelled isolated work before reclaiming its worktree.

    A paused Goal keeps its checkout so `/goal resume` has the exact worker
    state. Terminal outcomes cannot resume, so retaining a binary patch is
    the durable recovery artifact before freeing the checkout.
    """
    if state.change_mode != "worktree" or not state.change_worktree:
        return
    try:
        session = ChangeSession.attach(
            state.change_repository_root or state.workspace,
            state.change_worktree,
            base_commit=state.change_base_commit,
            worker_base_commit=state.change_baseline_commit,
            execution_relative=state.change_execution_relpath or ".",
            session_id=state.change_session_id or state.id,
        )
        archive_path = archive_goal_change_patch(state, session.export_goal_patch())
    except (ChangeSessionError, OSError) as exc:
        supervision = state.supervision if isinstance(state.supervision, dict) else {}
        state.supervision = {
            **supervision,
            "change_cleanup": "retained",
            "change_cleanup_error": str(exc),
            "change_cleanup_updated_at": time.time(),
        }
        save_goal(state)
        return

    state.change_archive_path = str(archive_path)
    state.supervision = {
        **(state.supervision if isinstance(state.supervision, dict) else {}),
        "change_cleanup": "archived",
        "change_cleanup_updated_at": time.time(),
    }
    save_goal(state)
    try:
        session.remove()
    except ChangeSessionError as exc:
        supervision = state.supervision if isinstance(state.supervision, dict) else {}
        state.supervision = {
            **supervision,
            "change_cleanup": "retained",
            "change_cleanup_error": str(exc),
            "change_cleanup_updated_at": time.time(),
        }
        save_goal(state)
        return
    state.change_worktree = ""
    state.change_session_id = ""
    state.change_merge_state = "archived"
    save_goal(state)


def start_goal(request: GoalRequest, *, history: list, context: dict, binding: Any) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        try:
            existing = load_goal()
        except GoalStoreError as exc:
            if exc.code != "unsupported_schema":
                raise
            # Starting a new Goal is an explicit request for the current
            # contract. Preserve the old document as history rather than
            # pretending it can safely resume under a different authority
            # model.
            archive_unsupported_goal(get_workdir())
            existing = None
        if _runner is not None and _runner.is_alive() and _runner.is_running():
            raise GoalBusyError("A goal is already running. Use /goal status, pause, or cancel.")
        if existing and existing.status not in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
            raise GoalBusyError(f"Goal {existing.id} is {existing.status}. Resume, cancel, or finish it first.")
        if existing:
            if existing.status in {GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                _finalize_terminal_change_session(existing)
            archive_goal(existing)
        workspace_root = get_workdir().resolve()
        try:
            execution_workspace = Path(request.execution_workspace or workspace_root).expanduser().resolve()
        except OSError as exc:
            raise ValueError(f"invalid Goal execution workspace: {exc}") from exc
        if not execution_workspace.is_dir() or not execution_workspace.is_relative_to(workspace_root):
            raise ValueError("Goal execution workspace must be an existing directory inside the current workspace")
        goal_id = f"goal_{time.time_ns()}_{hashlib.sha1(request.target.encode('utf-8')).hexdigest()[:4]}"
        change_session, isolated_execution_workspace = _create_goal_change_session(
            workspace_root, execution_workspace, goal_id
        )
        state = GoalState.new(
            target=request.target, verification=request.verification, workspace=str(workspace_root),
            execution_workspace=str(isolated_execution_workspace),
            workspace_generation=workspace_generation(), evaluation_required=request.evaluation_required,
            worker_round_limit=request.worker_round_limit,
            operation_timeout_seconds=request.operation_timeout_seconds,
            draft_id=request.draft_id,
        )
        if change_session is not None:
            state.change_mode = "worktree"
            state.change_session_id = change_session.session_id
            state.change_worktree = str(change_session.execution_worktree)
            state.change_base_commit = change_session.base_commit
            state.change_baseline_commit = change_session.worker_base_commit
            state.change_repository_root = str(change_session.repository_root)
            state.change_execution_relpath = change_session.execution_relative
        if request.task_plan:
            state.task_plan = [dict(item) for item in request.task_plan]
        state.goal_contract = dict(request.goal_contract or {
            "target": request.target,
            "autonomy": "After /goal run, resolve ordinary requirement ambiguity from the contract and repository evidence without asking the user.",
        })
        review = state.goal_contract.get("planning_review")
        state.planning_review = dict(review) if isinstance(review, dict) else {}
        state.execution_approved = not request.await_execution_approval
        problems = validate_limits(state)
        if problems:
            if change_session is not None:
                try:
                    change_session.remove()
                except ChangeSessionError:
                    pass
            raise ValueError("invalid goal limits: " + "; ".join(problems))
        try:
            lease_token = acquire_goal_lease(state)
        except GoalLeaseError as exc:
            if change_session is not None:
                try:
                    change_session.remove()
                except ChangeSessionError:
                    pass
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
            if change_session is not None:
                try:
                    change_session.remove()
                except ChangeSessionError:
                    pass
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
            _execution_workspace(state)
        except ChangeSessionError as exc:
            state.last_error = str(exc)
            state.stop_reason = StopReason.change_session_unavailable.value
            save_goal(state)
            raise GoalNotRunningError(f"Goal isolation is unavailable: {exc}") from exc
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
            elif not state.execution_approved:
                # Older draft-backed Goals stopped after every Task had a test
                # binding.  Lazy preparation instead approves the frozen
                # contract once and lets each runnable Task bind its test when
                # it is selected. A non-approval pause from that legacy flow
                # must therefore resume execution rather than rebuilding a
                # future Task's tests first.
                state.execution_approved = True

            # Pauses are outside the active execution budget. This also makes
            # a draft safely resumable the next day after test review.
            if state.paused_at is not None:
                state.started_at += max(0.0, time.time() - state.paused_at)
                state.paused_at = None

            target = _resume_target(state)
            _consume_supervisor_recovery(state)
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
    candidate = _supervisor_recovery_target(state) or state.resume_phase or GoalPhase.SELECT_TASK.value
    allowed = {
        phase.value for phase in (
            GoalPhase.PREPARE_TESTS, GoalPhase.SELECT_TASK, GoalPhase.PREPARE_EXECUTION, GoalPhase.CLAIM, GoalPhase.ACT,
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
    if (
        state.stop_reason == StopReason.repair_limit_reached.value
        and candidate == GoalPhase.REPAIR_PLAN.value
        and current is not None
        and current.status == "in_progress"
    ):
        from harness.verification.snapshot import capture_code_snapshot

        # A human or a recovery tool may have changed the implementation while
        # the Goal was paused. Verify that concrete change before spending a
        # fresh worker turn; otherwise a repaired Task can be sent back into
        # the same exhausted loop without ever observing its new evidence.
        current_snapshot = capture_code_snapshot(_execution_workspace(state))
        if current.start_snapshot and current_snapshot and current_snapshot != current.start_snapshot:
            state.repair_attempts = 0
            state.repair_epoch += 1
            state.no_progress_count = 0
            state.consecutive_failures = 0
            state.last_error = "Workspace changed after the repair pause; verify the current Task before another worker."
            candidate = GoalPhase.VERIFY.value
        else:
            # An explicit user resume starts a new, bounded repair epoch. The old
            # repair history remains audit evidence, but routing back into the
            # exhausted repair checkpoint would pause immediately without ever
            # verifying the current implementation.
            state.repair_attempts = 0
            state.repair_epoch += 1
            state.no_progress_count = 0
            state.consecutive_failures = 0
            state.last_error = "Repair budget reset after explicit resume; verify the current Task."
            candidate = GoalPhase.ACT.value
    # A recoverable pause can happen after verification/evaluation already
    # passed (for example, a stale scope snapshot). Do not spend another model
    # turn re-running the implementation worker; continue from the next
    # deterministic gate instead.
    if current is not None and current.status == "in_progress" and current.verification_state == "passing":
        if state.evaluation_required and (current.evaluation or {}).get("passed") is True:
            candidate = GoalPhase.CLEAN_CHECK.value
        elif state.evaluation_required and candidate in {GoalPhase.ACT.value, GoalPhase.REPAIR_PLAN.value}:
            candidate = GoalPhase.EVALUATE.value
    task_phases = {
        GoalPhase.PREPARE_EXECUTION.value,
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
            return (
                GoalPhase.IMPACT_REVIEW.value
                if candidate in {GoalPhase.CLEAN_CHECK.value, GoalPhase.IMPACT_REVIEW.value}
                else GoalPhase.SELECT_TASK.value
            )
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
        # A paused repair checkpoint contains the only durable repair
        # direction. Claiming first would silently discard it and send a
        # worker back to the same failed approach.
        if candidate in {GoalPhase.ACT.value, GoalPhase.ROLLOVER.value} and current.status == "pending":
            return GoalPhase.CLAIM.value
        return candidate

    # SELECT_TASK will find the first runnable test-generation task.  Do not
    # preempt the saved checkpoint merely because a blocked future Task exists.
    return candidate


def _supervisor_recovery_target(state: GoalState) -> str | None:
    """Return a fresh, deterministic terminal-supervisor recovery target.

    Terminal supervision runs after a runner stops, so it cannot safely move a
    Goal back to ``running`` by itself.  Instead it leaves one versioned
    directive which the normal resume path consumes.  The revision guard makes
    an old observation advisory only after any later state transition.
    """
    supervision = state.supervision if isinstance(state.supervision, dict) else {}
    directive = supervision.get("recovery")
    if not isinstance(directive, dict) or directive.get("consumed_at"):
        return None
    try:
        revision = int(directive.get("transition_revision"))
    except (TypeError, ValueError):
        return None
    if revision != len(state.transition_log):
        return None
    action = str(directive.get("action") or "")
    target = str(directive.get("target_phase") or "")
    if action not in {"retry", "redirect", "replan"}:
        return None
    if target not in {
        GoalPhase.PREPARE_TESTS.value,
        GoalPhase.SELECT_TASK.value,
        GoalPhase.CLAIM.value,
        GoalPhase.ACT.value,
        GoalPhase.ROLLOVER.value,
        GoalPhase.VERIFY.value,
        GoalPhase.EVALUATE.value,
        GoalPhase.REPAIR_PLAN.value,
        GoalPhase.IMPACT_REVIEW.value,
        GoalPhase.CLEAN_CHECK.value,
        GoalPhase.FULL_VERIFY.value,
    }:
        return None
    return target


def _consume_supervisor_recovery(state: GoalState) -> None:
    """Mark the one-shot directive consumed before a new runner is launched."""
    supervision = state.supervision if isinstance(state.supervision, dict) else {}
    directive = supervision.get("recovery")
    if not isinstance(directive, dict) or _supervisor_recovery_target(state) is None:
        return
    state.supervision = {
        **supervision,
        "recovery": {**directive, "consumed_at": time.time()},
    }


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
            _finalize_terminal_change_session(state)
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
        self._supervisor: ParallelGoalSupervisor | None = None
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

    def _start_supervisor(self) -> None:
        from harness.agents.registry import get_agent_profile, validate_agent_model

        try:
            route_error = validate_agent_model("goal_supervisor")
            if route_error:
                raise ValueError(route_error)
            self._supervisor = ParallelGoalSupervisor(
                cwd=_execution_workspace(self._state),
                operation_timeout_seconds=self._state.operation_timeout_seconds,
                cancel_check=self._interrupted,
            )
            profile = get_agent_profile("goal_supervisor")
            current = self._state.supervision if isinstance(self._state.supervision, dict) else {}
            self._state.supervision = {
                **current,
                "status": "observing",
                "model": profile.model_id if profile is not None else "goal_supervisor",
                "error": "",
                "updated_at": time.time(),
                "history": list(current.get("history") or [])[-12:],
            }
            save_goal(self._state)
            _emit("goal_supervisor", goal_id=self._state.id, event="started", **self._state.supervision)
        except Exception as exc:
            self._supervisor = None
            current = self._state.supervision if isinstance(self._state.supervision, dict) else {}
            profile = get_agent_profile("goal_supervisor")
            self._state.supervision = {
                **current,
                "status": "unavailable",
                "model": profile.model_id if profile is not None else "goal_supervisor",
                "error": f"goal supervisor could not start: {type(exc).__name__}: {exc}",
                "updated_at": time.time(),
                "history": list(current.get("history") or [])[-12:],
            }
            save_goal(self._state)
            _emit(
                "goal_supervisor",
                goal_id=self._state.id,
                event="unavailable",
                **self._state.supervision,
            )

    @staticmethod
    def _goal_scope_envelope(state: GoalState) -> tuple[str, ...]:
        from harness.tasks import load_task

        values: list[str] = []
        for plan in state.task_plan:
            if not isinstance(plan, dict):
                continue
            for key in ("primary_write", "planned_new", "conditional_write"):
                scopes = plan.get(key) if isinstance(plan.get(key), list) else []
                values.extend(str(item) for item in scopes if str(item).strip())
        for task_id in state.task_ids:
            try:
                task = load_task(task_id)
                values.extend(str(item) for item in [*task.primary_write, *task.planned_new, *task.conditional_write] if str(item).strip())
            except (FileNotFoundError, OSError, TypeError, ValueError):
                continue
        normalized = []
        for item in values:
            value = item.replace("\\", "/").strip()
            while value.startswith("./"):
                value = value[2:]
            normalized.append(value)
        return tuple(dict.fromkeys(item for item in normalized if item))

    @staticmethod
    def _agent_stats_detail(
        agent_type: str,
        stats: AgentTaskStats,
        *,
        summary: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "agent_type": str(agent_type),
            "summary": str(summary or "")[:2_000],
            "stop_reason": str(stats.stop_reason or ""),
            "llm_rounds": max(0, int(stats.llm_rounds)),
            "elapsed_seconds": max(0.0, float(stats.elapsed_seconds)),
            "tool_count": max(0, int(stats.tool_count)),
            "tool_names": list(dict.fromkeys(str(item) for item in stats.tool_names))[-12:],
            "read_paths": list(dict.fromkeys(str(item) for item in stats.read_paths))[-12:],
            "write_paths": list(dict.fromkeys(str(item) for item in stats.write_paths))[-12:],
            "write_outcomes": list(dict.fromkeys(str(item) for item in stats.write_outcomes))[-12:],
            "write_audits": list(dict.fromkeys(str(item) for item in stats.write_audits))[-12:],
            "tool_errors": [str(item)[:500] for item in stats.tool_errors[-6:]],
            **dict(extra or {}),
        }

    @staticmethod
    def _scope_candidate(
        workspace: str | Path,
        raw_path: str,
    ) -> tuple[str | None, Path | None, str | None]:
        root = Path(workspace).expanduser().resolve()
        value = str(raw_path or "").strip()
        if not value:
            return None, None, "scope request omitted its path"
        try:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve()
            relative = candidate.relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            return None, None, f"scope request is outside the execution workspace: {value!r} ({exc})"
        if not relative or relative == ".":
            return None, None, "the execution workspace root cannot be granted as write scope"
        protected = {".git", ".project", ".features"}
        for part in Path(relative).parts:
            lowered = part.casefold()
            if lowered in protected or lowered.startswith(".env"):
                return None, None, f"protected Goal path cannot be granted: {relative}"
        return relative, candidate, None

    def _scope_expansion_paths(
        self,
        state: GoalState,
        run: SupervisorRun,
        requests: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[str, ...], str | None]:
        supervision = state.supervision if isinstance(state.supervision, dict) else {}
        try:
            current_revision = int(supervision.get("observation_revision") or 0)
        except (TypeError, ValueError):
            current_revision = 0
        if run.revision != current_revision:
            return (), "the supervisor suggestion is stale"
        requested: set[str] = set()
        for request in requests:
            tool = str(request.get("tool") or "")
            if tool not in {"write_file", "edit_file", "patch_file"}:
                return (), f"{tool or 'unknown tool'} cannot receive automatic scope expansion"
            relative, _, error = self._scope_candidate(
                _execution_workspace(state),
                str(request.get("path") or ""),
            )
            if error:
                return (), error
            assert relative is not None
            requested.add(relative)

        proposed: list[tuple[str, Path]] = []
        for path in run.decision.scope_paths:
            relative, candidate, error = self._scope_candidate(_execution_workspace(state), path)
            if error:
                return (), error
            assert relative is not None and candidate is not None
            if relative not in requested:
                return (), f"supervisor proposed a path that was not actually requested: {relative}"
            proposed.append((relative, candidate))
        if not proposed:
            return (), "supervisor requested scope expansion without an exact path"

        envelope: list[tuple[str, Path]] = []
        for path in self._goal_scope_envelope(state):
            relative, candidate, error = self._scope_candidate(_execution_workspace(state), path)
            if error is None and relative is not None and candidate is not None:
                envelope.append((relative, candidate))
        approved: list[str] = []
        for relative, candidate in proposed:
            inside = any(
                relative == envelope_relative
                or (envelope_path.is_dir() and candidate.is_relative_to(envelope_path))
                for envelope_relative, envelope_path in envelope
            )
            if not inside:
                return (), f"requested path is outside the approved Goal scope envelope: {relative}"
            approved.append(relative)
        return tuple(dict.fromkeys(approved)), None

    def _scope_amendment_paths(
        self,
        state: GoalState,
        task: Any,
        run: SupervisorRun,
        requests: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[str, ...], str | None]:
        """Prove a missing Task source path is required by an unchanged bound test.

        This is deliberately narrower than a normal scope expansion: it repairs
        a planner omission without changing product intent, and only after the
        supervisor has explicitly selected ``amend_scope`` with high confidence.
        """
        if run.decision.confidence != "high":
            return (), "scope amendment requires high supervisor confidence"
        requested: set[str] = set()
        for request in requests:
            if str(request.get("tool") or "") not in {"write_file", "edit_file", "patch_file"}:
                return (), "only direct file writes can receive a scope amendment"
            relative, _, error = self._scope_candidate(
                _execution_workspace(state), str(request.get("path") or "")
            )
            if error:
                return (), error
            assert relative is not None
            requested.add(relative)
        if not requested:
            return (), "scope amendment has no exact requested path"

        proposed: set[str] = set()
        for raw_path in run.decision.scope_paths:
            relative, candidate, error = self._scope_candidate(_execution_workspace(state), raw_path)
            if error:
                return (), error
            assert relative is not None and candidate is not None
            if relative not in requested:
                return (), f"supervisor proposed a path that was not actually requested: {relative}"
            if not candidate.is_file() or candidate.suffix.lower() not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                return (), f"scope amendment requires an existing source file: {relative}"
            proposed.add(relative)
        if not proposed:
            return (), "scope amendment needs one exact source path"

        imported = self._frozen_test_imports(state, task)
        missing = sorted(proposed - imported)
        if missing:
            return (), "scope amendment path is not directly imported by an unchanged bound test: " + ", ".join(missing)
        return tuple(sorted(proposed)), None

    def _frozen_test_imports(self, state: GoalState, task: Any) -> set[str]:
        """Return source modules directly imported by unchanged Task-bound tests."""
        spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
        hashes = spec.get("test_hashes") if isinstance(spec.get("test_hashes"), dict) else {}
        root = Path(_execution_workspace(state)).resolve()
        imported: set[str] = set()
        for raw_test_path in spec.get("test_files") or []:
            test_relative, test_path, error = self._scope_candidate(root, str(raw_test_path))
            if error or test_relative is None or test_path is None or not test_path.is_file():
                continue
            expected_hash = str(hashes.get(test_relative) or "")
            actual_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
            if not expected_hash or actual_hash != expected_hash:
                continue
            try:
                source = test_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Match module specifiers independently of the imported bindings.
            # Named imports are commonly formatted across several lines, so a
            # one-line ``import {...} from`` pattern silently omitted exactly
            # the production module that a frozen test was proving necessary.
            for match in re.finditer(
                r"\bfrom\s*[\"']([^\"']+)[\"']|\bimport\s*[\"']([^\"']+)[\"']|\brequire\s*\(\s*[\"']([^\"']+)[\"']",
                source,
            ):
                raw_import = next((value for value in match.groups() if value), "")
                if not raw_import.startswith("."):
                    continue
                base = (test_path.parent / raw_import).resolve()
                candidates = [base]
                if base.suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
                    candidates.extend(base.with_suffix(suffix) for suffix in (".ts", ".tsx", ".js", ".jsx"))
                for candidate in candidates:
                    try:
                        if candidate.is_file() and candidate.is_relative_to(root):
                            imported.add(candidate.relative_to(root).as_posix())
                    except OSError:
                        continue
            # Python tests generally name the production module with an
            # absolute dotted import (``from harness.goal.runner import ...``).
            # Resolve only a matching workspace module; standard-library and
            # third-party imports cannot expand a Task's write scope.
            for match in re.finditer(
                r"(?m)^\s*(?:from\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\b|"
                r"import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))",
                source,
            ):
                module = next((value for value in match.groups() if value), "")
                if not module:
                    continue
                candidate = root.joinpath(*module.split(".")).with_suffix(".py")
                try:
                    if candidate.is_file() and candidate.is_relative_to(root):
                        imported.add(candidate.relative_to(root).as_posix())
                except OSError:
                    continue
        return imported

    def _verification_scope_omission_candidates(self, state: GoalState, task: Any) -> tuple[str, ...]:
        scope: list[Path] = []
        for raw_path in task.scope_paths or []:
            _, candidate, error = self._scope_candidate(_execution_workspace(state), str(raw_path))
            if error is None and candidate is not None:
                scope.append(candidate)
        root = Path(_execution_workspace(state)).resolve()
        candidates: list[str] = []
        for relative in self._frozen_test_imports(state, task):
            candidate = (root / relative).resolve()
            if any(candidate == allowed or candidate.is_relative_to(allowed) for allowed in scope):
                continue
            candidates.append(relative)
        return tuple(sorted(dict.fromkeys(candidates)))

    def _try_verification_scope_amendment(self, state: GoalState, task: Any) -> bool:
        """Repair a test-proven Task scope omission before returning to ACT.

        The supervisor still observes and explains this boundary, but it is not
        an authority bottleneck.  An unchanged, Task-bound test directly
        importing an existing source file is mechanical evidence that the Task
        plan omitted that file.  Requiring a second model to restate that fact
        turned a safe reconciliation into a provider/JSON single point of
        failure and let stale no-progress counters win instead.
        """
        from harness.tasks import save_task

        candidates = self._verification_scope_omission_candidates(state, task)
        if not candidates:
            return False
        requests = tuple({
            "tool": "patch_file",
            "path": path,
            "resource": path,
            "reason": "frozen bound test imports an out-of-scope source module after verification failed",
            "source": "verification_scope_reconciliation",
        } for path in candidates)
        run = self._review_supervisor_boundary(
            "verification_scope_omission",
            detail={
                "requests": [dict(item) for item in requests],
                "candidates": list(candidates),
                "latest_verification": (task.evidence[-1] if task.evidence else {}),
            },
        )
        paths: tuple[str, ...] = ()
        error: str | None = None
        source = "deterministic test evidence"
        if run is not None and not run.decision.unavailable and run.decision.action == "amend_scope":
            paths, error = self._scope_amendment_paths(state, task, run, requests)
            source = "global supervisor plus deterministic test evidence"
        else:
            paths, error = self._test_proven_scope_amendment_paths(state, task, candidates)
        if error:
            return False
        self._record_scope_amendment(state, task, paths)
        task.last_error = f"Task scope reconciled from {source}."
        save_task(task)
        state.last_error = task.last_error
        state.no_progress_count = 0
        state.permission_boundary_attempts.pop(task.id, None)
        save_goal(state)
        _emit_goal("goal_status", state)
        self._observe_supervisor(
            "verification_scope_amended",
            detail={"task_id": task.id, "paths": list(paths), "evidence": "unchanged bound test import"},
        )
        self._apply(state, GoalPhase.ACT, "verification_scope_amended")
        return True

    def _test_proven_scope_amendment_paths(
        self,
        state: GoalState,
        task: Any,
        candidates: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str | None]:
        """Validate a narrow autonomous scope correction from frozen test imports.

        This deliberately has no Goal-envelope exception: it can add only a
        real source file that the unchanged bound test imports directly.  It
        cannot grant tests, configuration, secret files, directories, or a
        transitive/guessed dependency.
        """
        imported = self._frozen_test_imports(state, task)
        approved: list[str] = []
        for raw_path in candidates:
            relative, candidate, error = self._scope_candidate(
                _execution_workspace(state), raw_path
            )
            if error:
                return (), error
            assert relative is not None and candidate is not None
            if relative not in imported:
                return (), f"scope amendment path is not directly imported by an unchanged bound test: {relative}"
            if not candidate.is_file() or candidate.suffix.lower() not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                return (), f"scope amendment requires an existing source file: {relative}"
            approved.append(relative)
        if not approved:
            return (), "scope amendment has no test-proven source path"
        return tuple(sorted(dict.fromkeys(approved))), None

    @staticmethod
    def _record_scope_amendment(state: GoalState, task: Any, paths: tuple[str, ...]) -> None:
        task.scope_paths = list(dict.fromkeys([*task.scope_paths, *paths]))
        task.primary_write = list(dict.fromkeys([*task.primary_write, *paths]))
        task.conditional_write = [path for path in task.conditional_write if path not in paths]
        for plan in state.task_plan:
            if not isinstance(plan, dict) or plan.get("name") != task.subject:
                continue
            primary = plan.get("primary_write") if isinstance(plan.get("primary_write"), list) else []
            conditional = plan.get("conditional_write") if isinstance(plan.get("conditional_write"), list) else []
            plan["primary_write"] = list(dict.fromkeys([*primary, *paths]))
            plan["conditional_write"] = [path for path in conditional if path not in paths]
            if isinstance(plan.get("scope_paths"), list):
                plan["scope_paths"] = list(dict.fromkeys([*plan["scope_paths"], *paths]))
            break

    def _handle_permission_boundary(
        self,
        state: GoalState,
        task: Any,
        *,
        requests: tuple[dict[str, Any], ...],
        agent_detail: dict[str, Any],
    ) -> None:
        from harness.tasks import save_task

        attempts = dict(state.permission_boundary_attempts or {})
        try:
            prior_attempts = int(attempts.get(task.id, 0))
        except (TypeError, ValueError):
            prior_attempts = 0
        attempt = max(0, prior_attempts) + 1
        attempts[task.id] = attempt
        state.permission_boundary_attempts = attempts
        save_goal(state)
        run = self._review_supervisor_boundary(
            "permission_boundary",
            detail={
                "attempt": attempt,
                "max_attempts": MAX_PERMISSION_BOUNDARY_RETRIES,
                "requests": [dict(item) for item in requests],
                "agent": agent_detail,
            },
        )

        def pause(message: str) -> None:
            task.last_error = message
            save_task(task)
            state.last_error = message
            self._pause(
                state,
                "goal_permission_required",
                stop_reason=StopReason.permission_wait.value,
            )
            current = state.supervision if isinstance(state.supervision, dict) else {}
            state.supervision = {
                **current,
                "terminal_boundary_revision": len(state.transition_log),
            }
            save_goal(state)

        def pause_supervisor_unavailable(message: str) -> None:
            task.last_error = message
            save_task(task)
            state.last_error = message
            # No user authority can resolve a model timeout. Keep the blocked
            # write auditable, but surface the real provider condition so a
            # resume retries supervision instead of asking for approval.
            attempts.pop(task.id, None)
            state.permission_boundary_attempts = attempts
            self._pause(
                state,
                "goal_supervisor_unavailable",
                stop_reason=StopReason.provider_unavailable.value,
            )
            save_goal(state)

        if run is None:
            pause_supervisor_unavailable("Global supervisor is unavailable; the requested capability remains blocked.")
            return
        decision = run.decision
        if decision.unavailable:
            pause_supervisor_unavailable(decision.error or "Global supervisor could not analyze the permission boundary.")
            return
        supervision = state.supervision if isinstance(state.supervision, dict) else {}
        try:
            current_revision = int(supervision.get("observation_revision") or 0)
        except (TypeError, ValueError):
            current_revision = 0
        if run.revision != current_revision:
            pause("Global supervisor returned a stale permission decision; no authority was changed.")
            return

        guidance = decision.next_step or decision.reason or decision.summary
        if decision.action in {"continue", "watch"}:
            # The write hook may have blocked an exploratory edit even though
            # the worker subsequently completed its scoped approach.  A
            # supervisor ``continue``/``watch`` decision means no expanded
            # authority is required, so do not leave that stale request
            # holding the Goal in permission_wait.
            attempts.pop(task.id, None)
            state.permission_boundary_attempts = attempts
            clear_goal_permission_flags()
            state.no_progress_count = 0
            save_goal(state)
            self._observe_supervisor(
                "permission_request_resolved_in_scope",
                detail={
                    "task_id": task.id,
                    "action": decision.action,
                    "attempt": attempt,
                    "guidance": guidance,
                },
            )
            self._apply(state, GoalPhase.VERIFY, "permission_request_resolved_in_scope")
            return
        if decision.action in {"expand_scope", "amend_scope"}:
            if attempt > MAX_PERMISSION_BOUNDARY_RETRIES:
                pause(
                    f"Task reached {MAX_PERMISSION_BOUNDARY_RETRIES} automatic permission-boundary retries. "
                    f"Latest supervisor analysis: {decision.summary}"
                )
                return
            if decision.action == "amend_scope":
                paths, error = self._scope_amendment_paths(state, task, run, requests)
            else:
                paths, error = self._scope_expansion_paths(state, run, requests)
            if error:
                pause(f"Scope {decision.action} was rejected by deterministic policy: {error}")
                return
            if decision.action == "amend_scope":
                self._record_scope_amendment(state, task, paths)
                task.last_error = f"Global supervisor corrected a Task scope omission: {guidance}"
            else:
                task.scope_paths = list(dict.fromkeys([*task.scope_paths, *paths]))
                task.last_error = f"Global supervisor expanded Task scope: {guidance}"
            save_task(task)
            state.last_error = task.last_error
            state.no_progress_count = 0
            save_goal(state)
            _emit_goal("goal_status", state)
            self._observe_supervisor(
                "permission_scope_amended" if decision.action == "amend_scope" else "permission_scope_expanded",
                detail={"task_id": task.id, "paths": list(paths), "attempt": attempt, "action": decision.action},
            )
            return
        if decision.action in {"redirect", "retry"}:
            if attempt > MAX_PERMISSION_BOUNDARY_RETRIES:
                pause(
                    f"Task reached {MAX_PERMISSION_BOUNDARY_RETRIES} automatic permission-boundary retries. "
                    f"Latest supervisor analysis: {decision.summary}"
                )
                return
            task.last_error = f"Global supervisor {decision.action}: {guidance}"
            save_task(task)
            state.last_error = task.last_error
            state.no_progress_count = 0
            save_goal(state)
            _emit_goal("goal_status", state)
            self._observe_supervisor(
                "permission_worker_redirected",
                detail={"task_id": task.id, "action": decision.action, "attempt": attempt},
            )
            return
        if decision.action == "replan":
            task.last_error = f"Global supervisor requested replanning: {guidance}"
            save_task(task)
            state.last_error = task.last_error
            self._apply(state, GoalPhase.REPAIR_PLAN, "supervisor_permission_replan", error=task.last_error)
            return
        pause(
            decision.summary
            or "The permission request needs authority outside the current Goal contract."
        )

    def _supervisor_observation(
        self,
        event: str,
        *,
        detail: dict[str, Any] | None = None,
        boundary: bool = False,
    ) -> dict[str, Any]:
        from harness.tasks import load_task

        state = self._state
        task_payload: dict[str, Any] | None = None
        if state.current_task_id:
            try:
                task = load_task(state.current_task_id)
                task_payload = {
                    "id": task.id,
                    "subject": task.subject,
                    "status": task.status,
                    "verification_state": task.verification_state,
                    "primary_write": list(task.primary_write),
                    "planned_new": list(task.planned_new),
                    "conditional_write": list(task.conditional_write),
                    "acceptance_cases": list(task.acceptance_cases),
                    "last_error": task.last_error,
                    "verification_spec": dict(task.verification_spec),
                    "latest_evidence": (
                        dict(task.evidence[-1]) if task.evidence and isinstance(task.evidence[-1], dict) else None
                    ),
                }
            except (FileNotFoundError, OSError, TypeError, ValueError):
                task_payload = {"id": state.current_task_id, "error": "Task state unavailable"}
        supervision = state.supervision if isinstance(state.supervision, dict) else {}
        try:
            prior_revision = int(supervision.get("observation_revision") or 0)
        except (TypeError, ValueError):
            prior_revision = 0
        revision = prior_revision + 1
        state.supervision = {
            **supervision,
            "observation_revision": revision,
        }
        history = supervision.get("history") if isinstance(supervision.get("history"), list) else []
        return {
            "event": str(event),
            "boundary": bool(boundary),
            "revision": revision,
            "at": time.time(),
            "goal": {
                "id": state.id,
                "target": state.target,
                "phase": state.phase,
                "status": state.status,
                "resume_phase": state.resume_phase,
                "stop_reason": state.stop_reason,
                "last_error": state.last_error,
                "contract": state.goal_contract,
                "scope_envelope": list(self._goal_scope_envelope(state)),
            },
            "task": task_payload,
            "recent_transitions": list(state.transition_log[-10:]),
            "prior_supervision": list(history[-6:]),
            "detail": dict(detail or {}),
        }

    def _observe_supervisor(self, event: str, *, detail: dict[str, Any] | None = None) -> None:
        if self._supervisor is None or event not in ROUTINE_SUPERVISION_EVENTS:
            return
        observation = self._supervisor_observation(event, detail=detail)
        observation_id = self._supervisor.observe(observation)
        current = self._state.supervision if isinstance(self._state.supervision, dict) else {}
        current_status = str(current.get("status") or "")
        self._state.supervision = {
            **current,
            "status": current_status if current_status in {"attention", "unavailable"} else "observing",
            "observed_event": event,
            "observation_id": observation_id,
            "observed_revision": observation["revision"],
            "updated_at": time.time(),
        }
        save_goal(self._state)
        _emit(
            "goal_supervisor",
            goal_id=self._state.id,
            event="observing",
            observation_id=observation_id,
            observed_event=event,
            revision=observation["revision"],
        )

    def _record_supervisor_run(self, run: SupervisorRun, *, event: str) -> None:
        decision = run.decision.to_dict()
        current = self._state.supervision if isinstance(self._state.supervision, dict) else {}
        try:
            current_revision = int(current.get("observation_revision") or 0)
        except (TypeError, ValueError):
            current_revision = 0
        stale = run.revision < current_revision
        record = {
            **decision,
            "trigger": event,
            "observation_id": run.observation_id,
            "revision": run.revision,
            "stale": stale,
            "at": time.time(),
        }
        history = list(current.get("history") or [])
        history.append(record)
        status = str(current.get("status") or "observing") if stale else (
            "unavailable" if run.decision.unavailable else (
                "attention" if run.decision.action not in {"continue", "watch"} else "observing"
            )
        )
        latest = current.get("latest") if stale and isinstance(current.get("latest"), dict) else record
        updated_supervision = {
            **current,
            "status": status,
            "latest": latest,
            "history": history[-12:],
            "updated_at": record["at"],
        }
        if event == "terminal_failure" and not stale and not run.decision.unavailable:
            directive = self._terminal_recovery_directive(run)
            if directive is not None:
                updated_supervision["recovery"] = directive
        self._state.supervision = updated_supervision
        self._state.total_llm_rounds += max(0, int(run.llm_rounds))
        save_goal(self._state)
        _emit(
            "goal_supervisor",
            goal_id=self._state.id,
            event="decision",
            **record,
        )

    def _terminal_recovery_directive(self, run: SupervisorRun) -> dict[str, Any] | None:
        """Translate only safe terminal advice into a one-shot resume route."""
        action = run.decision.action
        if action not in {"retry", "redirect", "replan"}:
            return None
        if action == "replan":
            target = GoalPhase.REPAIR_PLAN.value if self._state.current_task_id else GoalPhase.SELECT_TASK.value
        else:
            target = self._state.resume_phase or self._state.last_phase or GoalPhase.SELECT_TASK.value
        guidance = run.decision.next_step or run.decision.reason or run.decision.summary
        return {
            "action": action,
            "target_phase": target,
            "guidance": guidance[:4_000],
            "task_id": self._state.current_task_id,
            "observation_id": run.observation_id,
            "revision": run.revision,
            "transition_revision": len(self._state.transition_log),
            "created_at": time.time(),
        }

    def _poll_supervisor(self) -> None:
        if self._supervisor is None:
            return
        for result in self._supervisor.poll():
            self._record_supervisor_run(result, event="parallel_observation")

    def _review_supervisor_boundary(
        self,
        event: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> SupervisorRun | None:
        if self._supervisor is None:
            return None
        self._poll_supervisor()
        result = self._supervisor.review(
            self._supervisor_observation(event, detail=detail, boundary=True)
        )
        self._record_supervisor_run(result, event=event)
        return result

    def _review_terminal_supervision(self) -> None:
        state = self._state
        if state.status not in {GoalStatus.PAUSED.value, GoalStatus.FAILED.value}:
            return
        last_transition = state.transition_log[-1] if state.transition_log else {}
        if state.stop_reason in {
            StopReason.user_approval_required.value,
            StopReason.cancelled_by_user.value,
        } or last_transition.get("reason") == "user_pause":
            return
        terminal_boundary_revision = (
            state.supervision.get("terminal_boundary_revision")
            if isinstance(state.supervision, dict)
            else None
        )
        try:
            reviewed_revision = int(terminal_boundary_revision or -1)
        except (TypeError, ValueError):
            reviewed_revision = -1
        if reviewed_revision == len(state.transition_log):
            return
        self._review_supervisor_boundary(
            "terminal_failure",
            detail={
                "phase": state.resume_phase or state.last_phase or state.phase,
                "stop_reason": state.stop_reason,
                "error": state.last_error,
            },
        )

    def _close_supervisor(self) -> None:
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.close()

    def run(self) -> None:
        self._start_supervisor()
        try:
            self._drive()
        except Exception as exc:
            self._fail(self._state, StopReason.internal_error, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self._poll_supervisor()
                self._review_terminal_supervision()
            finally:
                self._close_supervisor()
                try:
                    if self._state.status in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                        if self._state.status in {GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                            _finalize_terminal_change_session(self._state)
                        archive_goal(self._state)
                finally:
                    release_goal_lease(self._state, self._lease_token)
            _emit_goal("goal_stopped", self._state)

    def _drive(self) -> None:
        while self.is_running():
            state = self._state
            self._poll_supervisor()
            if workspace_generation() != state.workspace_generation:
                self._fail(state, StopReason.workspace_changed, "workspace switched while goal was active")
                return
            if self._cancel_event.is_set():
                self._cancel(state, "user requested cancel")
                return
            if self._pause_event.is_set() and not self._phase_in_flight:
                self._pause(state, "user_pause")
                return
            try:
                _execution_workspace(state)
            except ChangeSessionError as exc:
                state.last_error = str(exc)
                self._pause(
                    state,
                    "change_session_unavailable",
                    stop_reason=StopReason.change_session_unavailable.value,
                )
                return
            self._step_once(state)

    def _step_once(self, state: GoalState) -> None:
        if state.phase == GoalPhase.INITIALIZE.value:
            self._initialize(state)
        elif state.phase == GoalPhase.PREPARE_TESTS.value:
            self._prepare_tests(state)
        elif state.phase == GoalPhase.PREPARE_EXECUTION.value:
            self._prepare_execution(state)
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
                    compiled_plan = plan_tasks(
                        state.target, state.verification, root,
                        cancel_check=self._interrupted, deadline=self._deadline(state), stats=stats,
                        human_language=human_language_label((state.goal_contract or {}).get("language")),
                    )
                except Exception as exc:
                    state.total_llm_rounds += stats.llm_rounds
                    state.last_error = f"Goal planner unavailable: {type(exc).__name__}: {exc}"
                    self._observe_supervisor(
                        "agent_finished",
                        detail=self._agent_stats_detail(
                            "goal_planner",
                            stats,
                            summary=state.last_error,
                        ),
                    )
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
            self._observe_supervisor(
                "agent_finished",
                detail=self._agent_stats_detail(
                    "goal_planner",
                    stats,
                    summary=f"Planner and independent reviewer accepted {len(compiled_plan.tasks)} Task contracts.",
                ),
            )
            if self._honor_control_request(state):
                return
            state.goal_contract = {
                **dict(state.goal_contract or {}),
                **dict(compiled_plan.contract),
            }
            state.planning_review = dict(compiled_plan.review)
            state.task_plan = [item.to_dict() for item in compiled_plan.tasks]
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
                primary_write=list(plan.get("primary_write") or []),
                planned_new=list(plan.get("planned_new") or []),
                conditional_write=list(plan.get("conditional_write") or []),
                read_envelope=list(plan.get("read_envelope") or []),
                forbidden=list(plan.get("forbidden") or []),
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
        """Generate and bind tests for one runnable Task at a time.

        A future Task's difficult test seam must not block implementation of a
        Task that already has a valid red baseline.  The active Task is tried
        first so repair/test-gap checkpoints retain their ownership.
        """
        from harness.goal.memory import record_test_binding
        from harness.tasks import bind_task_verification, load_task

        root = Path(_execution_workspace(state))
        deferred = False
        task_ids = (
            (state.current_task_id,)
            if state.current_task_id
            else tuple(task_id for task_id in state.task_ids if task_id)
        )
        for task_id in task_ids:
            task = load_task(task_id)
            if task.verification_state != "needs_generation":
                continue
            # A dependent Task's red baseline is meaningful only after its
            # prerequisites exist. Otherwise Task A's absence can be falsely
            # attributed to Task B.
            if any(load_task(dep).status != "completed" for dep in task.blockedBy):
                deferred = True
                continue
            # Every generated artifact, checkpoint, supervisor snapshot, and
            # pause must identify the Task that is actually being prepared.
            state.current_task_id = task.id
            save_goal(state)
            adapter = select_adapter(root, task.verification_spec.get("adapter") or state.verification)
            verification_context = VerificationContext(root, command=state.verification)
            before_catalog = (
                collect_pytest_catalog(root)
                if adapter.id == "pytest"
                else adapter.discover(verification_context)
            )
            write_roots = self._test_write_roots(before_catalog)
            before_tree = self._snapshot_test_tree(root, write_roots)
            test_read_roots: list[str] = [*write_roots, "docs"]
            test_read_paths = ["package.json", "tsconfig.json", "tsconfig.ink.json", "bunfig.toml"]
            # Test writers inspect the pre-approved read envelope, not merely
            # the paths that the implementation worker may edit or create.
            for raw_path in [*task.read_envelope, *task.primary_write]:
                relative, candidate, error = self._scope_candidate(root, str(raw_path))
                if error is not None or relative is None or candidate is None or not candidate.exists():
                    continue
                (test_read_roots if candidate.is_dir() else test_read_paths).append(relative)
            test_read_roots = list(dict.fromkeys(test_read_roots))
            test_read_paths = list(dict.fromkeys(test_read_paths))
            impact_context = task.verification_spec.get("impact_context") or []
            if not isinstance(impact_context, list):
                impact_context = []
            if adapter.id == "node":
                selector_example = "test/example.test.ts::behavior name"
                adapter_test_guidance = (
                    "For Node, use test/**/*.test.ts for non-JSX tests or test/**/*.test.tsx only when JSX is "
                    "required; both extensions are machine-collected. Do not put JSX in a .test.ts file. "
                    "Create the focused test first and let the project runner settle syntax/runtime questions instead "
                    "of spending a slice debating TypeScript rules. Return the exact discovered test name after the file path. "
                )
            else:
                selector_example = "tests/test_x.py::test_name"
                adapter_test_guidance = ""
            response_example = json.dumps({
                "test_selectors": [selector_example],
                "case_selectors": {"AC1": [selector_example]},
                "test_design": [{
                    "layer": "pure_logic",
                    "target": "src/module.ts",
                    "seam": "named observable behavior",
                    "cases": ["AC1"],
                    "runner": "project-declared runner",
                }],
            })
            pure_logic_required = self._requires_pure_logic_test(task)
            test_architecture_guidance = (
                "Classify the test before writing it. Use pure_logic for key bindings, cursor/text editing, "
                "queues, reducers, parsing, formatting, and state transitions; use component only for rendered "
                "component behavior; use terminal_integration only when native terminal rendering is itself the acceptance case. "
                "For pure_logic, import the smallest existing production module in the approved scope. Never import "
                "a complete application entry such as App.tsx, index.tsx, or a native renderer just to reach a helper. "
                "Use the project's declared test runner; do not invent a Node loader. "
                "Include a test_design array with layer, target, seam, cases, and runner in your final JSON."
            )
            if pure_logic_required:
                test_architecture_guidance += (
                    " This Task is a pure-logic Task. Its generated test must not import App.tsx, index.tsx, "
                    "or another complete UI entry point."
                )
            prompt = (
                f"Create a NEW focused {adapter.id} test file for this Task before implementation. You may modify only test files; "
                f"do not edit existing test files or production code. Use existing test conventions. {adapter_test_guidance}"
                "The test file must be machine-collectable even though its pre-implementation baseline is expected "
                "to fail on the missing behavior. An explanation or empty selector list is not a valid result. "
                "Test-design protocol: inspect the relevant source and existing tests before choosing a boundary. "
                "For a broad Task, make multiple focused test groups when its acceptance cases cross pure state/data, "
                "async coordination, input routing, and rendering. Each group must state its layer, existing target module, "
                "observed seam, and covered acceptance IDs in test_design. Do not invent a new production API or module just "
                "to make a test fail; assert missing behavior through an existing observable boundary. "
                "Your read boundary excludes dependency trees such as node_modules: use the Task source, existing tests, "
                "docs, and project configuration as the evidence base. "
                f"After writing tests, reply ONLY with JSON: {response_example}.\n\n"
                f"Test architecture rules: {test_architecture_guidance}\n\n"
                f"Task: {task.subject}\nBehavior: {task.description}\nAcceptance cases: {json.dumps(task.acceptance_cases)}"
            )
            prompt += (
                "\n\nWrite only human-facing test descriptions and your final summary in "
                f"{human_language_label((state.goal_contract or {}).get('language'))}. "
                "Keep JSON keys, test selectors, paths, commands, and code unchanged."
            )
            recovery = (state.supervision or {}).get("recovery") if isinstance(state.supervision, dict) else None
            if (
                isinstance(recovery, dict)
                and recovery.get("guidance")
                and recovery.get("task_id") == task.id
                and recovery.get("target_phase") == GoalPhase.PREPARE_TESTS.value
            ):
                prompt += (
                    "\n\nGlobal supervisor recovery direction (follow it only within the frozen Task and test-only "
                    f"boundary): {str(recovery['guidance'])[:4_000]}"
                )
            if task.test_strategy:
                prompt += f"\nPlanning test strategy: {task.test_strategy}"
            if task.primary_write or task.planned_new or task.conditional_write:
                prompt += "\nTask scope classes: " + json.dumps({
                    "primary_write": task.primary_write,
                    "planned_new": task.planned_new,
                    "conditional_write": task.conditional_write,
                    "read_envelope": task.read_envelope,
                })
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
            writer_conversation = AgentTaskConversation()
            writer_has_test_artifact = False
            def invoke_writer(active_prompt: str, description: str, _slice: int, stats: AgentTaskStats) -> str:
                self._phase_in_flight = True
                try:
                    with goal_authority(
                        goal_id=state.id,
                        task_id=task.id,
                        phase=GoalPhase.PREPARE_TESTS.value,
                        workspace=root,
                        write_roots=write_roots,
                    ):
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
                            read_roots=tuple(test_read_roots),
                            read_paths=tuple(test_read_paths),
                            conversation=writer_conversation,
                        )
                finally:
                    set_goal_noninteractive(False)
                    self._phase_in_flight = False

            def test_progress(previous: dict[str, bytes], current: dict[str, bytes], stats: AgentTaskStats) -> StageProgress:
                nonlocal writer_has_test_artifact
                created = sorted(set(current) - set(previous))
                changed = sorted(path for path in set(current) & set(previous) if current[path] != previous[path])
                all_created = sorted(set(current) - set(before_tree))
                writer_has_test_artifact = bool(all_created)
                # A writer commonly creates a focused test in one slice and
                # finishes its assertions in the next. Changes to a file that
                # was created during this attempt are real progress too.
                changed_new_artifacts = [path for path in changed if path not in before_tree]
                advanced = bool(created or changed_new_artifacts)
                if changed and not created and changed_new_artifacts:
                    summary = "refined a focused test artifact: " + ", ".join(changed_new_artifacts[:4])
                elif changed and not created:
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
                        "new_test_artifacts": all_created[:12],
                        "changed_existing_test_files": changed[:12],
                        "write_paths": stats.write_paths[-12:],
                        "tool_errors": stats.tool_errors[-4:],
                    },
                )

            def on_test_slice(item: Any) -> None:
                state.total_llm_rounds += item.stats.llm_rounds
                self._observe_supervisor(
                    "agent_slice_finished",
                    detail=self._agent_stats_detail(
                        "goal_test_writer",
                        item.stats,
                        summary=item.progress.summary,
                        extra={
                            "task_id": task.id,
                            "slice": item.number,
                            "idle_slices": item.idle_slices,
                            "checkpoint": dict(item.progress.checkpoint),
                        },
                    ),
                )

            def test_writer_continuation(_slice: int, progress: StageProgress, idle_slices: int) -> str:
                if not writer_has_test_artifact:
                    return (
                        "The source-inspection and test-design slice is complete. Do not reopen the architecture decision "
                        "or continue discussing alternatives, TypeScript syntax, or JSX extension choices. Use the existing conversation evidence and your selected "
                        "source-grounded test_design now. Your next tool call must create a NEW syntactically valid focused "
                        "test file under the approved test root, then collect it and return selector JSON with test_design. "
                        "For Node, write a non-JSX .test.ts file unless JSX is genuinely required; in that case write .test.tsx. "
                        "Do not wait for a theoretical answer about either extension: run the declared test command and use its output. "
                        "The test must exercise an existing observable boundary, not an invented production API. "
                        "Do not modify production code or existing test files. "
                        f"Supervisor evidence: {progress.summary}; idle slices: {idle_slices}."
                    )
                return (
                    "Continue the same test-writing conversation. All prior assistant messages, source contents, "
                    "and tool results remain available above. Use that retained evidence to finish the focused "
                    "machine-collectable test and submit non-empty selector JSON with test_design. Do not invent a "
                    "production API merely to finish the test: choose an existing observable boundary, or split the "
                    "tests into focused groups by layer. The missing product behavior should make the baseline fail; "
                    "it is not a reason to return an empty result. Re-read source when useful. "
                    f"Supervisor evidence: {progress.summary}. Do not modify existing test files."
                )

            supervised = StageSupervisor(StagePolicy(
                name="test_generation",
                slice_rounds=TEST_WRITER_MAX_ROUNDS,
                max_idle_slices=TEST_WRITER_MAX_IDLE_CHUNKS,
            )).run(
                invoke=invoke_writer,
                initial_prompt=prompt,
                initial_description=f"generate tests for task {task.id}",
                continuation_prompt=test_writer_continuation,
                continuation_description=lambda slice_number: f"continue generating tests for task {task.id} (slice {slice_number})",
                snapshot=lambda: self._snapshot_test_tree(root, write_roots),
                assess_progress=test_progress,
                on_slice=on_test_slice,
                continue_when=lambda raw, stats, _progress: (
                    stats.stop_reason == "completed"
                    and not writer_has_test_artifact
                    and not self._requested_selectors_from_generation(raw)
                ),
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
                or (
                    str(raw).startswith("[goal_test_writer] failed:")
                    and not empty_response
                    and not writer_stalled
                    and stats.stop_reason != "max_rounds"
                )
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
            generated_selectors = tuple(
                selector for selector in after_catalog.selectors
                if selector not in before_catalog.selectors
            )
            generated_selector_set = set(generated_selectors)
            required_cases = {
                str(case.get("id")) for case in task.acceptance_cases
                if isinstance(case, dict) and case.get("id")
            }
            selectors = self._selectors_from_generation(raw, root, catalog=after_catalog)
            case_selectors = self._case_selectors_from_generation(raw, selectors, task.acceptance_cases)
            test_design = self._test_design_from_generation(raw)
            requested_selectors = self._requested_selectors_from_generation(raw)

            def contract_mismatches(*, response_empty: bool, response_stalled: bool) -> list[str]:
                mismatches: list[str] = []
                if response_empty:
                    mismatches.append("test writer returned an empty response")
                if response_stalled:
                    mismatches.append("test writer stalled before submitting selector JSON")
                if not requested_selectors:
                    mismatches.append("response did not contain test_selectors")
                if requested_selectors and not selectors:
                    mismatches.append(f"returned selectors did not resolve in the {adapter.id} catalog")
                unexpected = [selector for selector in selectors if selector not in generated_selector_set]
                if unexpected:
                    mismatches.append(
                        "returned selectors were existing or non-generated: " + ", ".join(unexpected)
                    )
                missing_cases = sorted(required_cases - set(case_selectors))
                if missing_cases:
                    mismatches.append("case_selectors did not cover: " + ", ".join(missing_cases))
                return mismatches

            original_empty_response = empty_response
            original_writer_stalled = writer_stalled
            initial_requested_selectors = requested_selectors
            initial_mismatches = contract_mismatches(
                response_empty=empty_response,
                response_stalled=writer_stalled,
            )
            # A tool-free selector repair is useful only after the machine has
            # actually collected a new selector. Empty lists cannot recover a
            # writer that never created a test artifact.
            repair_attempted = bool(initial_mismatches and generated_selectors)
            if repair_attempted:
                # The writer may have created a valid test but named it
                # differently in its final JSON. Give a tool-free completion
                # turn only the machine-observed new selectors, never the full
                # catalog or a model-invented fallback.
                completion_prompt = (
                    "The focused tests have already been written, but the prior selector JSON could not be safely bound. "
                    "Do not call tools or edit files. Reply ONLY with one JSON object mapping every acceptance case "
                    "to selectors from the machine-collected list below. Do not use any selector outside this list.\n\n"
                    f"Verification adapter: {adapter.id}\n"
                    f"Task: {task.subject}\n"
                    f"Acceptance cases: {json.dumps(task.acceptance_cases, ensure_ascii=False)}\n"
                    f"Machine-collected new selectors: {json.dumps(generated_selectors, ensure_ascii=False)}\n"
                    "The JSON must contain a test_selectors array and a case_selectors object whose keys are "
                    "acceptance-case IDs and whose values are selector arrays."
                )
                retry_stats = AgentTaskStats()
                self._phase_in_flight = True
                try:
                    set_goal_noninteractive(True)
                    raw = run_agent_task(
                        description=f"repair generated test selector binding for task {task.id}",
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
                self._observe_supervisor(
                    "agent_finished",
                    detail=self._agent_stats_detail(
                        "goal_test_writer",
                        retry_stats,
                        summary=str(raw)[-2_000:],
                        extra={"task_id": task.id, "source": "selector_binding_repair"},
                    ),
                )
                empty_response = retry_stats.stop_reason == "empty_response"
                selectors = self._selectors_from_generation(raw, root, catalog=after_catalog)
                case_selectors = self._case_selectors_from_generation(raw, selectors, task.acceptance_cases)
                requested_selectors = self._requested_selectors_from_generation(raw)

            final_mismatches = contract_mismatches(
                response_empty=empty_response,
                response_stalled=False,
            )

            def mismatch_diagnostic() -> str:
                details: dict[str, Any] = {
                    "adapter": adapter.id,
                    "mismatch": "; ".join(final_mismatches) or "unknown selector contract mismatch",
                    "generated_selectors": list(generated_selectors),
                    "requested_selectors": list(requested_selectors),
                    "response_tail": str(raw)[-1_200:],
                }
                if repair_attempted:
                    details["initial_mismatch"] = "; ".join(initial_mismatches)
                    details["initial_requested_selectors"] = list(initial_requested_selectors)
                return json.dumps(details, ensure_ascii=False)

            if not generated_selectors and (original_empty_response or original_writer_stalled):
                stop_reason = (
                    StopReason.test_writer_stalled.value
                    if original_writer_stalled
                    else StopReason.test_writer_empty_response.value
                )
                detail = (
                    f"Test writer reached {TEST_WRITER_MAX_IDLE_CHUNKS} consecutive round slices without "
                    f"creating a new collected test artifact for Task {task.id}."
                    if original_writer_stalled
                    else (
                        f"Test writer used tools but did not submit a final result for Task {task.id}; "
                        "no machine-collected new test selector was available to bind."
                    )
                )
                state.last_error = f"{detail} Diagnostic: {mismatch_diagnostic()}"
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(
                    state,
                    "goal_test_writer_stalled" if original_writer_stalled else "goal_test_writer_empty_response",
                    stop_reason=stop_reason,
                )
                return
            if not selectors:
                state.last_error = (
                    f"Task {task.id} needs a machine-collected {adapter.id} selector before execution. "
                    f"Diagnostic: {mismatch_diagnostic()}"
                )
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_required", stop_reason=StopReason.test_generation_required.value)
                return
            if not required_cases.issubset(case_selectors):
                state.last_error = (
                    f"Task {task.id} test writer must map every acceptance case to machine-collected selectors. "
                    f"Diagnostic: {mismatch_diagnostic()}"
                )
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_case_mapping_required", stop_reason=StopReason.test_generation_required.value)
                return
            if any(selector not in generated_selector_set for selector in selectors):
                state.last_error = (
                    f"Task {task.id} test writer reused an existing or non-generated selector; it must add focused coverage. "
                    f"Diagnostic: {mismatch_diagnostic()}"
                )
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_reused_existing_selector", stop_reason=StopReason.test_generation_required.value)
                return
            files = tuple(dict.fromkeys(item.split("::", 1)[0] for item in selectors))
            architecture_error = self._generated_test_architecture_error(root, task, files)
            if architecture_error:
                self._restore_test_tree(root, before_tree, write_roots, expected_after=self._snapshot_test_tree(root, write_roots))
                self._pause(state, "test_generation_architecture_invalid", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} generated an invalid test architecture: {architecture_error}"
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
                controller_authorized=True,
            )
            if self._honor_control_request(state):
                return
            output = str(getattr(baseline, "stdout", "") or "")
            infrastructure_failure = (
                "error collecting" in output.lower()
                or "importerror" in output.lower()
                or "fixture" in output.lower() and "error" in output.lower()
            ) and not self._is_expected_planned_new_module_import_failure(task, output)
            posthoc = bool(task.verification_spec.get("allow_posthoc_test"))
            if baseline.error or baseline.timed_out or (baseline.passed and not posthoc) or infrastructure_failure:
                detail = baseline.error or (
                    "generated tests passed before implementation; they do not prove the missing behavior"
                    if baseline.passed
                    else "generated test baseline failed outside the requested behavior"
                )
                output_tail = output[-1_200:].strip()
                if output_tail:
                    detail = f"{detail}. Verification output tail: {output_tail}"
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
            if test_design:
                bound_spec["test_design"] = list(test_design)
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
            # A successful retry must not leave the prior baseline diagnostic
            # visible while the Goal proceeds to implementation.
            state.last_error = None
            # Test preparation is deliberately lazy. The next Task is chosen
            # only after this one has been implemented and reviewed.
            break
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
    def _requested_selectors_from_generation(raw: str) -> tuple[str, ...]:
        try:
            text = str(raw or "")
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        except (ValueError, TypeError, json.JSONDecodeError):
            return ()
        requested = data.get("test_selectors") if isinstance(data, dict) else None
        if not isinstance(requested, list):
            return ()
        return tuple(str(item) for item in requested if item is not None)

    @staticmethod
    def _is_expected_planned_new_module_import_failure(task, output: str) -> bool:
        """Accept the red baseline for a test importing this Task's new module.

        Pytest reports this as a collection error before the module exists, but
        it is exactly the behavior a test-first Task with ``planned_new`` must
        demonstrate.  Other collection, fixture, and import failures remain
        invalid baselines.
        """
        text = str(output or "").replace("\\", "/").casefold()
        import_markers = (
            "importerror",
            "modulenotfounderror",
            "no module named",
            "cannot import name",
        )
        if not any(marker in text for marker in import_markers):
            return False
        for raw_path in getattr(task, "planned_new", ()) or ():
            path = str(raw_path).replace("\\", "/").lstrip("./")
            if not path.endswith(".py"):
                continue
            module = path[:-3].replace("/", ".").strip(".").casefold()
            if not module:
                continue
            modules = (module[:-9], module) if module.endswith(".__init__") else (module,)
            if any(candidate and candidate in text for candidate in modules):
                return True
        return False

    @staticmethod
    def _test_design_from_generation(raw: str) -> tuple[dict[str, Any], ...]:
        """Keep the writer's source-grounded test design with its bound evidence."""
        try:
            text = str(raw or "")
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        except (ValueError, TypeError, json.JSONDecodeError):
            return ()
        design = data.get("test_design") if isinstance(data, dict) else None
        entries = [design] if isinstance(design, dict) else design if isinstance(design, list) else []
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            layer = str(entry.get("layer") or "").strip()
            target = str(entry.get("target") or "").replace("\\", "/").strip()
            seam = str(entry.get("seam") or "").strip()
            cases = [str(case) for case in entry.get("cases", []) if str(case)] if isinstance(entry.get("cases"), list) else []
            runner = str(entry.get("runner") or "").strip()
            if layer and target and seam and cases:
                result.append({"layer": layer, "target": target, "seam": seam, "cases": cases, "runner": runner})
        return tuple(result)

    @staticmethod
    def _requires_pure_logic_test(task) -> bool:
        """Identify Tasks whose proof should not load a full UI application."""
        text = " ".join(
            str(value or "")
            for value in (getattr(task, "subject", ""), getattr(task, "description", ""), getattr(task, "test_strategy", ""))
        ).casefold()
        markers = (
            "keybinding", "keyboard", "shortcut", "cursor", "kill-ring", "text editing",
            "editing command", "queue", "reducer", "parser", "formatting", "state transition",
            "快捷键", "键盘", "光标", "编辑", "队列", "状态转换",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _generated_test_architecture_error(cls, root: Path, task, files: tuple[str, ...]) -> str | None:
        """Reject only a known-bad unit-test boundary for pure logic Tasks."""
        if not cls._requires_pure_logic_test(task):
            return None
        blocked = re.compile(
            r"(?:from\s+|import\s*(?:\([^)]*)?)[\"'][^\"']*(?:src-open/)?(?:App|index|main)\.(?:tsx?|jsx?)[\"']",
            re.IGNORECASE,
        )
        for rel in files:
            try:
                content = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if blocked.search(content):
                return (
                    f"pure-logic test {rel} imports a complete application entry. "
                    "Test the smallest production logic module instead."
                )
        return None

    @staticmethod
    def _selectors_from_generation(raw: str, workspace: Path, *, catalog=None) -> tuple[str, ...]:
        requested = GoalRunner._requested_selectors_from_generation(raw)
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
            if task.status == "blocked":
                state.current_task_id = task_id
                state.last_error = task.last_error or f"Task {task.id} is blocked"
                self._record_execution_trace(
                    state, "task_blocked", task_id=task.id, route="blocked", summary=state.last_error,
                )
                self._pause(state, "blocked_task_selected", stop_reason=StopReason.task_blocked.value)
                return
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
            self._apply(state, GoalPhase.PREPARE_EXECUTION, "task_selected")
            return
        self._apply(state, GoalPhase.FULL_VERIFY, "all_tasks_completed")

    def _record_execution_trace(
        self,
        state: GoalState,
        event: str,
        *,
        task_id: str | None = None,
        route: str = "",
        summary: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Persist compact, user-visible execution facts for supervision/UI."""
        entry = {
            "at": time.time(),
            "event": event,
            "task_id": task_id or state.current_task_id,
            "route": route,
            "summary": str(summary or "")[:1_500],
            "detail": dict(detail or {}),
        }
        state.execution_trace.append(entry)
        del state.execution_trace[:-80]

    def _prepare_execution(self, state: GoalState) -> None:
        """Check a frozen Task contract before an implementation worker starts."""
        from harness.tasks import load_task, record_task_evaluation, save_task

        task = load_task(state.current_task_id)
        if task.verification_state == "needs_generation":
            self._record_execution_trace(
                state, "execution_preflight", task_id=task.id, route="test_gap",
                summary="Focused Task tests are not bound yet; routing to test preparation.",
            )
            save_goal(state)
            self._apply(state, GoalPhase.PREPARE_TESTS, "execution_preflight_needs_tests")
            return

        errors: list[str] = []
        if any(load_task(dep).status != "completed" for dep in task.blockedBy):
            errors.append("Task dependencies are not completed")
        contract = state.goal_contract if isinstance(state.goal_contract, dict) else {}
        unresolved = contract.get("unresolved") if isinstance(contract.get("unresolved"), list) else []
        if unresolved:
            errors.append("Goal contract still has unresolved decisions")
        if not task.primary_write and not task.planned_new:
            errors.append("Task has no approved writable path")
        writes = [*task.primary_write, *task.planned_new]
        if set(writes).intersection(task.forbidden):
            errors.append("Task writable paths overlap forbidden paths")
        root = Path(_execution_workspace(state))
        for path in [*task.primary_write, *task.read_envelope, *task.conditional_write]:
            relative, candidate, error = self._scope_candidate(root, str(path))
            if error or relative is None or candidate is None or not candidate.exists():
                errors.append(f"Contract path is unavailable: {path}")
        for path in task.planned_new:
            relative, candidate, error = self._scope_candidate(root, str(path))
            if error or relative is None or candidate is None:
                errors.append(f"Planned new path is invalid: {path}")
            elif candidate.exists():
                errors.append(f"Planned new path already exists: {path}")

        record = {
            "task_id": task.id,
            "checked_at": time.time(),
            "passed": not errors,
            "errors": errors[:12],
            "verification_preconditions": list(contract.get("verification_preconditions") or [])[:12],
            "scope": {
                "primary_write": list(task.primary_write),
                "planned_new": list(task.planned_new),
                "conditional_write": list(task.conditional_write),
                "read_envelope": list(task.read_envelope),
            },
        }
        state.execution_preflight[task.id] = record
        if errors:
            task.last_error = "Execution preflight failed: " + "; ".join(errors[:4])
            save_task(task)
            record_task_evaluation(task.id, {
                "passed": False, "route": "replan", "summary": task.last_error,
                "findings": [{"issue": error, "severity": "high", "evidence": "execution preflight"} for error in errors[:6]],
            })
            self._record_execution_trace(state, "execution_preflight", task_id=task.id, route="replan", summary=task.last_error, detail=record)
            save_goal(state)
            self._apply(state, GoalPhase.REPAIR_PLAN, "execution_preflight_failed", error=task.last_error)
            return
        self._record_execution_trace(
            state, "execution_preflight", task_id=task.id, route="continue",
            summary="Task contract, dependency state, and execution scope are ready.", detail=record,
        )
        save_goal(state)
        self._apply(state, GoalPhase.ACT if task.status == "in_progress" else GoalPhase.CLAIM, "execution_preflight_passed")

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
        read_roots: list[str] = []
        read_paths: list[str] = ["package.json", "pyproject.toml", "pytest.ini", "tsconfig.json", "bunfig.toml"]
        for raw_path in [*task.read_envelope, *task.primary_write]:
            relative, candidate, error = self._scope_candidate(_execution_workspace(state), str(raw_path))
            if error is not None or relative is None or candidate is None or not candidate.exists():
                continue
            (read_roots if candidate.is_dir() else read_paths).append(relative)
        spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
        read_paths.extend(str(path) for path in spec.get("test_files") or [] if str(path).strip())
        read_roots = list(dict.fromkeys(read_roots))
        read_paths = list(dict.fromkeys(read_paths))
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
            with goal_authority(
                goal_id=state.id,
                task_id=task.id,
                phase=GoalPhase.ACT.value,
                workspace=_execution_workspace(state),
                write_roots=tuple(task.scope_paths),
            ) as authority:
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
                    # Pass the same resolved roots used by permission_hook;
                    # never rebuild an independent path scope in the tool layer.
                    write_roots=tuple(str(path) for path in authority.write_roots) or None,
                    read_roots=tuple(read_roots),
                    read_paths=tuple(read_paths),
                )
        finally:
            set_goal_noninteractive(False)
            self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        state.attempts += 1
        permission_pending = goal_permission_pending()
        permission_requests = goal_permission_requests()
        agent_detail = self._agent_stats_detail(
            "goal_worker",
            stats,
            summary=summary,
            extra={"task_id": task.id, "worker_generation": state.worker_generation},
        )
        scope_error = self._validate_task_scope(state, task)
        if scope_error:
            self._observe_supervisor(
                "agent_finished",
                detail={**agent_detail, "scope_error": scope_error},
            )
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
        if not permission_pending:
            attempts = dict(state.permission_boundary_attempts or {})
            attempts.pop(task.id, None)
            state.permission_boundary_attempts = attempts
            self._observe_supervisor("agent_finished", detail=agent_detail)
        worker_progressed = self._progress_snapshot(state) != before
        state.no_progress_count = state.no_progress_count + 1 if not worker_progressed else 0
        resumable_slice = stats.stop_reason in {"max_rounds", "max_tokens"}
        self._record_execution_trace(
            state,
            "implementation_slice",
            task_id=task.id,
            route="extend" if resumable_slice and worker_progressed else ("verify" if worker_progressed else "failure_analysis"),
            summary=(
                "Observable Task progress; extending implementation with a fresh bounded slice."
                if resumable_slice and worker_progressed
                else ("Worker slice ended with observable progress; running verification." if worker_progressed
                      else "Worker made no observable progress; verification will classify the blocker.")
            ),
            detail={
                "stop_reason": stats.stop_reason,
                "rounds": stats.llm_rounds,
                "tools": stats.tool_count,
                "idle_slices": state.no_progress_count,
                "write_paths": agent_detail["write_paths"],
                "write_outcomes": agent_detail["write_outcomes"],
                "tool_errors": agent_detail["tool_errors"],
            },
        )
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
        write_handoff(
            state,
            task,
            phase=GoalPhase.VERIFY.value,
            summary=summary,
            execution=agent_detail,
        )
        if self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
        elif permission_pending:
            self._handle_permission_boundary(
                state,
                task,
                requests=permission_requests,
                agent_detail=agent_detail,
            )
        elif self._pause_event.is_set():
            self._pause(state, "user_pause")
        elif stats.stop_reason == "provider_error":
            state.last_error = summary[:4_000]
            self._pause(
                state,
                "goal_worker_provider_error",
                stop_reason=StopReason.provider_unavailable.value,
            )
        elif resumable_slice and worker_progressed:
            state.worker_rollovers += 1
            self._apply(state, GoalPhase.ROLLOVER, "worker_progress_extended")
        else:
            # Always verify after a worker slice, even when it made no new
            # write. Existing implementation may already satisfy the Task,
            # and skipping verification here used to create act -> repair
            # loops that never tested the current code.
            self._apply(state, GoalPhase.VERIFY, "goal_worker_finished")

    def _rollover(self, state: GoalState) -> None:
        """Checkpoint a long implementation slice before authorizing more work."""
        latest = state.execution_trace[-1] if state.execution_trace else {}
        if latest.get("event") == "implementation_slice" and latest.get("route") == "extend":
            self._record_execution_trace(
                state,
                "extension_checkpoint",
                route="verify",
                summary="Progress slice reached its limit; verify it before another implementation slice.",
            )
            self._apply(state, GoalPhase.VERIFY, "worker_progress_checkpoint")
            return
        self._apply(state, GoalPhase.VERIFY, "worker_handoff_saved")

    def _goal_worker_context(self) -> dict[str, Any]:
        """Keep project rules, never the user's stale chat state or Todo list."""
        allowed = {"project_instructions", "memories", "connected_mcp"}
        return {key: value for key, value in self._context.items() if key in allowed}

    def _classify_verification_failure(self, state: GoalState, task: Any) -> tuple[str, str]:
        """Choose a deterministic first route before asking the repair model."""
        if self._verification_scope_omission_candidates(state, task):
            return "scope_omission", "A frozen Task test directly imports a source path outside the current Task scope."
        if task.verification_state == "needs_generation":
            return "test_gap", "Task verification has no bound focused test."
        detail = str(task.last_error or "").lower()
        external_markers = ("docker daemon", "cannot connect to docker", "connection refused", "service unavailable", "missing credential", "api key")
        if any(marker in detail for marker in external_markers):
            return "external_blocked", "Verification depends on an unavailable external runtime or credential."
        if state.no_progress_count >= NO_PROGRESS_REPLAN_LIMIT:
            return "replan", "Consecutive implementation slices changed neither Task evidence nor scoped code."
        return "implementation_fix", "Bound verification failed after an implementation attempt."

    def _verify(self, state: GoalState) -> None:
        from harness.tasks import record_task_evaluation

        task = verify_task_command(
            state.current_task_id,
            workspace=_execution_workspace(state),
            timeout_s=state.operation_timeout_seconds,
            cancel_check=self._interrupted,
            controller_authorized=True,
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
        self._observe_supervisor(
            "task_verification_failed",
            detail={
                "task_id": task.id,
                "latest_verification": task.evidence[-1] if task.evidence else {},
            },
        )
        if self._try_verification_scope_amendment(state, task):
            return
        route, reason = self._classify_verification_failure(state, task)
        evaluation = {
            "passed": False,
            "route": route,
            "summary": reason,
            "findings": [{
                "issue": str(task.last_error or reason)[:2_000],
                "severity": "high" if route in {"scope_omission", "external_blocked", "replan"} else "medium",
                "evidence": "bound verification",
            }],
        }
        record_task_evaluation(task.id, evaluation)
        self._record_execution_trace(
            state, "verification_failure", task_id=task.id, route=route,
            summary=reason, detail={"error": str(task.last_error or "")[:2_000], "idle_slices": state.no_progress_count},
        )
        state.last_error = f"{reason} Verification: {task.last_error or 'failed'}"
        save_goal(state)
        # Every failed proof now enters the repair planner with a structured
        # route. It replaces the old direct ACT retry loop.
        self._apply(state, GoalPhase.REPAIR_PLAN, f"verification_failure_{route}", error=state.last_error)

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
        self._observe_supervisor(
            "agent_finished",
            detail=self._agent_stats_detail(
                "evaluator",
                stats,
                summary=str(
                    evaluation.get("summary")
                    or evaluation.get("error")
                    or evaluation.get("passed")
                    or "evaluation completed"
                ),
                extra={"task_id": state.current_task_id, "verdict": evaluation.get("passed")},
            ),
        )
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

    def _replan_remaining_tasks(
        self,
        state: GoalState,
        *,
        task: Any,
        evaluation: dict[str, Any],
        reason: str,
    ) -> None:
        """Recompile only unfinished work while preserving the frozen Goal boundary."""
        from harness.goal.discovery_store import load_manifest
        from harness.tasks import create_task, load_task, save_task

        try:
            current_tasks = [load_task(task_id) for task_id in state.task_ids]
        except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
            detail = f"Cannot replan because a Goal Task is unavailable: {exc}"
            self._record_execution_trace(state, "execution_replan", task_id=task.id, route="blocked", summary=detail)
            self._pause(state, "execution_replan_tasks_unavailable", stop_reason=StopReason.task_blocked.value)
            return

        completed = [candidate for candidate in current_tasks if candidate.status == "completed"]
        unfinished = [candidate for candidate in current_tasks if candidate.status != "completed"]
        completed_names = tuple(candidate.subject for candidate in completed)
        if len(set(completed_names)) != len(completed_names):
            detail = "Cannot replan because completed Task names are not unique. Start a new Goal with a fresh contract."
            self._record_execution_trace(state, "execution_replan", task_id=task.id, route="blocked", summary=detail)
            self._pause(state, "execution_replan_duplicate_completed_names", stop_reason=StopReason.task_blocked.value)
            return
        manifest = load_manifest(state.workspace, state.draft_id or state.id)
        if manifest is None:
            detail = "Cannot safely replan without the original planning evidence manifest; the frozen contract remains unchanged."
            self._record_execution_trace(state, "execution_replan", task_id=task.id, route="blocked", summary=detail)
            self._pause(state, "execution_replan_manifest_unavailable", stop_reason=StopReason.execution_preflight_failed.value)
            return

        root = Path(_execution_workspace(state))
        adapter = select_adapter(root, state.verification)
        stats = AgentTaskStats()
        self._phase_in_flight = True
        try:
            compiled = plan_tasks(
                state.target,
                state.verification,
                root,
                cancel_check=self._interrupted,
                deadline=self._deadline(state),
                stats=stats,
                discovery_manifest=manifest,
                verification_adapter=adapter,
                human_language=human_language_label((state.goal_contract or {}).get("language")),
                frozen_contract=dict(state.goal_contract or {}),
                completed_task_names=completed_names,
                replan_reason=json.dumps(
                    {
                        "summary": reason,
                        "evaluation": {
                            "summary": evaluation.get("summary"),
                            "route": evaluation.get("route"),
                            "findings": evaluation.get("findings", []),
                        },
                    },
                    ensure_ascii=False,
                )[:12_000],
            )
        except Exception as exc:
            state.total_llm_rounds += stats.llm_rounds
            detail = f"Execution replan was not accepted: {type(exc).__name__}: {exc}"
            self._observe_supervisor(
                "agent_finished",
                detail=self._agent_stats_detail("goal_planner", stats, summary=detail, extra={"stage": "execution_replan"}),
            )
            self._record_execution_trace(state, "execution_replan", task_id=task.id, route="blocked", summary=detail)
            self._pause(
                state,
                "execution_replan_unavailable",
                stop_reason=(
                    StopReason.provider_unavailable.value
                    if stats.stop_reason in {"provider_error", "configuration_error"}
                    else StopReason.execution_preflight_failed.value
                ),
            )
            return
        finally:
            self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        self._observe_supervisor(
            "agent_finished",
            detail=self._agent_stats_detail(
                "goal_planner",
                stats,
                summary=f"Execution replan and independent review accepted {len(compiled.tasks)} replacement Task contracts.",
                extra={"stage": "execution_replan", "task_id": task.id},
            ),
        )

        replacement_names = [item.name for item in compiled.tasks]
        if len(set(replacement_names)) != len(replacement_names) or set(replacement_names).intersection(completed_names):
            detail = "Execution replan reused a completed Task name; refusing to replace unfinished work."
            self._record_execution_trace(state, "execution_replan", task_id=task.id, route="blocked", summary=detail)
            self._pause(state, "execution_replan_name_conflict", stop_reason=StopReason.execution_preflight_failed.value)
            return

        # The new contracts were fully validated before any existing Task is
        # superseded. Completed Task ids remain dependency anchors; unfinished
        # tasks remain on disk as audit records but leave the active Goal graph.
        for candidate in unfinished:
            candidate.status = "cancelled"
            candidate.owner = None
            candidate.last_error = f"Superseded by execution replan: {reason or 'task boundaries changed'}"[:4_000]
            save_task(candidate)

        names = {candidate.subject: candidate.id for candidate in completed}
        replacement_ids: list[str] = []
        for item in compiled.tasks:
            dependencies = [names[name] for name in item.depends_on]
            replacement = create_task(
                item.name,
                item.behavior,
                dependencies,
                goal_id=state.id,
                acceptance_cases=[case.to_dict() for case in item.acceptance_cases],
                skill_names=list(item.skill_names),
                verification_spec=item.verification_spec.to_dict(),
                evaluation_required=state.evaluation_required,
                primary_write=list(item.primary_write),
                planned_new=list(item.planned_new),
                conditional_write=list(item.conditional_write),
                read_envelope=list(item.read_envelope),
                forbidden=list(item.forbidden),
                evidence_refs=list(item.evidence_refs),
                test_strategy=item.test_strategy,
                discovery_revision=item.discovery_revision,
            )
            names[item.name] = replacement.id
            replacement_ids.append(replacement.id)

        state.task_ids = [candidate.id for candidate in completed] + replacement_ids
        state.task_name_ids = names
        state.task_plan = [item.to_dict() for item in compiled.tasks]
        state.current_task_id = replacement_ids[0] if replacement_ids else None
        state.planning_review = {
            **dict(state.planning_review or {}),
            "execution_replan": dict(compiled.review),
        }
        self._record_execution_trace(
            state,
            "execution_replan",
            task_id=task.id,
            route="continue",
            summary=f"Replaced {len(unfinished)} unfinished Task contract(s) with {len(replacement_ids)} reviewed contract(s).",
            detail={
                "reason": reason[:1_500],
                "evaluation_route": str(evaluation.get("route") or ""),
                "completed_preserved": [candidate.id for candidate in completed],
                "superseded": [candidate.id for candidate in unfinished],
                "replacement": replacement_ids,
            },
        )
        save_goal(state)
        self._apply(state, GoalPhase.SELECT_TASK, "execution_replan_accepted")

    def _repair_plan(self, state: GoalState) -> None:
        from harness.goal.memory import append_decisions
        from harness.goal.repair import fallback_repair_decision, plan_task_repair
        from harness.tasks import load_task, record_task_repair, request_task_test_repair

        task = load_task(state.current_task_id)
        evaluation = task.evaluation or {"passed": False, "summary": state.last_error or "Goal verification failed", "route": "implementation_fix"}
        if state.repair_epoch > 0:
            repair_count = sum(
                1
                for item in task.repair_history
                if isinstance(item, dict) and item.get("repair_epoch") == state.repair_epoch
            )
        else:
            # Legacy Goals predate repair epochs; their complete history is
            # the only available circuit-breaker evidence.
            repair_count = len(task.repair_history)
        if repair_count >= MAX_REPAIR_ATTEMPTS_PER_TASK:
            # This is a diagnostic threshold, never an execution ceiling. A
            # large Goal may legitimately need many repairs; force the next
            # planner decision to reconsider task decomposition instead of
            # silently stopping at an arbitrary number.
            evaluation = {
                **evaluation,
                "route": "replan",
                "summary": (
                    f"{repair_count} prior repair decisions did not complete the Task; "
                    "reassess task boundaries, test seam, and architecture direction."
                ),
            }
            self._record_execution_trace(
                state, "repair_threshold", task_id=task.id, route="replan",
                summary=str(evaluation["summary"]), detail={"repair_count": repair_count},
            )
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
        if decision.unavailable:
            state.last_error = decision.error or "repair planner unavailable"
            format_error = state.last_error.startswith((
                "repair planner returned no JSON",
                "invalid repair JSON:",
                "repair planner output is not an object",
                "unsupported repair action:",
                "repair action needs instructions",
            ))
            if not format_error:
                self._pause(
                    state,
                    "repair_planner_unavailable",
                    stop_reason=StopReason.provider_unavailable.value,
                )
                return
            # A malformed model response is recoverable: use a bounded local
            # direction and let the normal repair-attempt/no-progress limits
            # prevent an endless loop.
            decision = fallback_repair_decision(
                state.last_error,
                route=str(evaluation.get("route") or "implementation_fix"),
            )
        self._observe_supervisor(
            "agent_finished",
            detail=self._agent_stats_detail(
                "goal_repair_planner",
                stats,
                summary=decision.summary or decision.instructions or decision.error or decision.action,
                extra={
                    "task_id": task.id,
                    "action": decision.action,
                    "format_fallback": decision.format_fallback,
                },
            ),
        )
        # Record both model decisions and deterministic format fallbacks so the
        # UI/audit trail explains why the current Task was resumed.
        record = {
            "evaluation": evaluation,
            **decision.to_dict(),
            "repair_epoch": state.repair_epoch,
            "at": time.time(),
        }
        if decision.format_fallback:
            record["event"] = "repair_planner_format_fallback"
        record_task_repair(task.id, record)
        if decision.assumptions:
            append_decisions(state, task, list(decision.assumptions), source="repair_planner")
        if decision.action == "blocked":
            from harness.tasks import block_task

            detail = decision.error or decision.summary or "repair planner blocked"
            block_task(task.id, error=detail)
            self._record_execution_trace(state, "repair_blocked", task_id=task.id, route="blocked", summary=detail)
            self._pause(state, "task_blocked", stop_reason=StopReason.task_blocked.value)
            return
        state.repair_attempts += 1
        state.last_error = decision.summary or None
        save_goal(state)
        if decision.action == "replan":
            self._replan_remaining_tasks(state, task=task, evaluation=evaluation, reason=decision.summary or decision.instructions)
            return
        if decision.action == "test_gap":
            request_task_test_repair(task.id)
            self._record_execution_trace(
                state, "repair_routed", task_id=task.id,
                route="test_gap",
                summary=decision.summary or decision.instructions,
            )
            self._apply(
                state,
                GoalPhase.PREPARE_TESTS,
                "repair_requires_test_coverage",
            )
            return
        self._apply(
            state,
            GoalPhase.SELECT_TASK if task.status == "pending" else GoalPhase.ACT,
            "repair_plan_ready",
        )

    def _clean_check(self, state: GoalState) -> None:
        from harness.tasks import complete_task, load_task, record_task_evaluation, save_task

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
        record_task_evaluation(task.id, {
            "passed": False,
            "route": "implementation_fix",
            "summary": "The Task proof passed, but the clean delivery gate rejected the current workspace state.",
            "findings": [{"issue": result, "severity": "high", "evidence": "clean check"}],
        })
        self._record_execution_trace(
            state, "clean_check_failure", task_id=task.id, route="implementation_fix",
            summary="Clean delivery gate failed; routing through repair analysis instead of retrying blindly.",
            detail={"error": result[:2_000]},
        )
        self._apply(state, GoalPhase.REPAIR_PLAN, "clean_check_failed", error=result)

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
        self._observe_supervisor(
            "agent_finished",
            detail=self._agent_stats_detail(
                "goal_test_impact",
                stats,
                summary=decision.reason or decision.action,
                extra={"task_id": completed.id, "action": decision.action},
            ),
        )
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
                controller_authorized=True,
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
            controller_authorized=True,
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

        if result.passed and state.change_mode == "worktree":
            try:
                session = ChangeSession.attach(
                    state.change_repository_root or state.workspace,
                    state.change_worktree,
                    base_commit=state.change_base_commit,
                    worker_base_commit=state.change_baseline_commit,
                    execution_relative=state.change_execution_relpath or ".",
                    session_id=state.change_session_id or state.id,
                )
                source_execution = (
                    Path(state.change_repository_root or state.workspace)
                    / (state.change_execution_relpath or ".")
                ).resolve()
                integration = session.prepare_integration(source_execution_workspace=source_execution)
            except ChangeSessionError as exc:
                detail = f"change session merge unavailable: {exc}"
                self._record_final_verification(state, status="blocked", error=detail)
                state.last_error = detail
                state.change_merge_state = "conflict"
                self._pause(state, "merge_conflict", stop_reason=StopReason.merge_conflict.value)
                return

            try:
                integrated = run_verification(
                    state.verification,
                    workspace=integration.execution_workspace,
                    timeout_s=state.operation_timeout_seconds,
                    cancel_check=self._interrupted,
                    controller_authorized=True,
                )
                if self._honor_control_request(state):
                    return
                self._record_final_verification(
                    state,
                    status="passed" if integrated.passed else "failed",
                    result=integrated,
                )
                if not integrated.passed:
                    detail = integrated.error or f"integrated verification failed with exit code {integrated.exit_code}"
                    state.last_error = detail
                    state.change_merge_state = "verification_failed"
                    self._pause(
                        state,
                        "merge_verification_failed",
                        stop_reason=StopReason.merge_verification_failed.value,
                    )
                    return
                state.change_merge_state = "publishing"
                state.supervision = {
                    **(state.supervision if isinstance(state.supervision, dict) else {}),
                    "merge_status": "verified",
                    "merge_session": state.change_session_id,
                    "merge_updated_at": time.time(),
                }
                save_goal(state)
                try:
                    merge_status = integration.publish()
                except ChangeSessionError as exc:
                    detail = f"verified Goal merge could not be published: {exc}"
                    state.last_error = detail
                    state.change_merge_state = "publish_unavailable"
                    state.supervision = {
                        **(state.supervision if isinstance(state.supervision, dict) else {}),
                        "merge_status": "publish_unavailable",
                        "merge_error": str(exc),
                        "merge_session": state.change_session_id,
                        "merge_updated_at": time.time(),
                    }
                    self._pause(state, "merge_publish_unavailable", stop_reason=StopReason.merge_conflict.value)
                    return
            finally:
                try:
                    integration.remove()
                except ChangeSessionError:
                    pass

            state.supervision = {
                **(state.supervision if isinstance(state.supervision, dict) else {}),
                "merge_status": merge_status,
                "merge_session": state.change_session_id,
                "merge_updated_at": time.time(),
            }
            if merge_status != "published":
                state.last_error = (
                    "Main workspace changed while the verified Goal merge was being published."
                    if merge_status == "main_changed"
                    else (
                        "Main workspace is busy with another harness writer; verified Goal changes were not applied."
                        if merge_status == "main_locked"
                        else "Verified Goal changes could not be applied to the main workspace."
                    )
                )
                state.change_merge_state = (
                    "main_changed_after_publish" if merge_status == "main_changed" else (
                        "main_locked" if merge_status == "main_locked" else "publish_conflict"
                    )
                )
                save_goal(state)
                self._pause(state, "merge_conflict", stop_reason=StopReason.merge_conflict.value)
                return
            state.change_merge_state = "published"
            self._apply(state, GoalPhase.DONE, "goal_verification_passed")
            try:
                session.remove()
            except ChangeSessionError:
                # Cleanup is best effort after the durable DONE transition.
                pass
            else:
                state.change_worktree = ""
                state.change_session_id = ""
                save_goal(state)
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
        root = Path(_execution_workspace(state))
        adapter = select_adapter(root, state.verification)
        decision = plan_goal_regression_repair(
            state,
            tasks,
            cwd=_execution_workspace(state),
            adapter_id=adapter.id,
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if self._honor_control_request(state):
            return
        self._observe_supervisor(
            "agent_finished",
            detail=self._agent_stats_detail(
                "goal_repair_planner",
                stats,
                summary=decision.summary or decision.instructions or decision.error or decision.action,
                extra={"action": decision.action, "source": "full_verification"},
            ),
        )
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
        selector = self._failed_verification_selector(
            str(final.get("stdout_tail") or ""),
            adapter=adapter,
            workspace=root,
            command=state.verification,
        )
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
        # concrete failing selector gives it a machine-verifiable gate.
        if selector is None:
            state.last_error = f"{detail}. The planner requested a repair Task but no failing {adapter.id} selector was recorded."
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
        catalog = (
            collect_pytest_catalog(root)
            if adapter.id == "pytest"
            else adapter.discover(VerificationContext(root, command=state.verification))
        )
        if not catalog.contains(selector):
            state.last_error = f"Failed selector {selector!r} is no longer collected; repair Task was not created."
            self._pause(state, "full_verification_repair_selector_unavailable", stop_reason=StopReason.full_verification_failed.value)
            return
        test_file = selector.split("::", 1)[0]
        name = f"goal regression repair {state.repair_attempts + 1}"
        verification_spec = {
            "adapter": adapter.id,
            "command": adapter.build_command((selector,)),
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

    @classmethod
    def _failed_verification_selector(cls, output: str, *, adapter, workspace: Path, command: str) -> str | None:
        """Return one collected failing selector without assuming pytest.

        pytest reports the full node id directly. Node's built-in runner reports
        a test title, so resolve that title against the adapter's discovered
        catalog and refuse ambiguous matches.
        """
        if adapter.id == "pytest":
            return cls._failed_pytest_selector(output)
        catalog = adapter.discover(VerificationContext(workspace, command=command))
        for line in output.splitlines():
            match = re.match(r"^(?:not ok\s+\d+\s+-|[✖x])\s*(.+?)(?:\s+\([^)]*\))?$", line.strip(), re.IGNORECASE)
            if not match:
                continue
            title = match.group(1).strip()
            matches = [
                selector for selector in catalog.selectors
                if selector.split("::", 1)[-1] == title
            ]
            if len(matches) == 1:
                return matches[0]
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
    def _scope_relative_path(state: GoalState, raw_path: str) -> str:
        """Normalize git-root and execution-workspace paths to one scope form."""
        value = str(raw_path or "").strip().replace("\\", "/")
        if not value:
            return ""
        execution = Path(_execution_workspace(state)).expanduser().resolve()
        repository = Path(state.workspace or execution).expanduser().resolve()
        try:
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                execution_prefix = ""
                try:
                    execution_prefix = execution.relative_to(repository).as_posix()
                except ValueError:
                    pass
                # Git commands run from a nested workspace can still report
                # paths relative to the repository root (for example,
                # node_tui/src-open/interaction.ts). Prefer that interpretation
                # when the path carries the execution workspace prefix.
                if execution_prefix and (value == execution_prefix or value.startswith(execution_prefix + "/")):
                    resolved = (repository / candidate).resolve()
                else:
                    resolved = (execution / candidate).resolve()
                    if not resolved.is_relative_to(execution):
                        resolved = (repository / candidate).resolve()
            if resolved.is_relative_to(execution):
                return resolved.relative_to(execution).as_posix()
        except (OSError, ValueError):
            pass
        return value.lstrip("./")

    @staticmethod
    def _validate_task_scope(state: GoalState, task) -> str | None:
        """Reject production changes outside the planner's declared scope.

        Files already dirty when the Task was claimed are exempt; they belong
        to the user's pre-existing work and are recorded in the Task baseline.
        """
        from harness.verification.snapshot import capture_dirty_file_hashes

        scope = {
            GoalRunner._scope_relative_path(state, str(path))
            for path in (task.scope_paths or [])
            if path
        }
        forbidden = {
            GoalRunner._scope_relative_path(state, str(path))
            for path in (getattr(task, "forbidden", None) or [])
            if path
        }
        if not scope:
            return None
        current = capture_dirty_file_hashes(_execution_workspace(state))
        baseline = {
            GoalRunner._scope_relative_path(state, str(path)): str(digest)
            for path, digest in (task.start_dirty_hashes or {}).items()
        }
        # Generated verification files are part of the Task contract even
        # when the planner's production scope only names source directories.
        # Without this allowance, a successful test-generation step is later
        # rejected as an autonomy violation by the worker scope gate.
        verification_spec = task.verification_spec if isinstance(task.verification_spec, dict) else {}
        test_scope = {
            GoalRunner._scope_relative_path(state, str(path))
            for path in (verification_spec.get("test_files") or [])
            if path
        }
        # A file that was already dirty is not exempt if this worker changed it
        # again. Compare digests rather than merely comparing path names.
        changed = {
            GoalRunner._scope_relative_path(state, str(path)): digest
            for path, digest in current.items()
            if baseline.get(GoalRunner._scope_relative_path(state, str(path))) != str(digest)
        }
        prohibited = sorted(
            path for path in changed
            if path in forbidden or any(path.startswith(item.rstrip("/") + "/") for item in forbidden)
        )
        if prohibited:
            return "worker changed forbidden Task paths: " + ", ".join(prohibited[:12])
        outside = sorted(
            path
            for path in changed
            if path not in test_scope
            and path not in scope
            and not any(path.startswith(item.rstrip("/") + "/") for item in scope)
            and not any(
                part.lower() in {"test", "tests", "__tests__"}
                for part in Path(path).parts
            )
        )
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
        source = state.phase
        self.engine.transition(state, target, reason, error=error, stop_reason=stop_reason)
        save_goal(state)
        _emit_goal("goal_phase", state)
        self._observe_supervisor(
            "phase_transition",
            detail={
                "from": source,
                "to": state.phase,
                "reason": reason,
                "error": error,
                "stop_reason": stop_reason,
            },
        )

    def _pause(self, state: GoalState, reason: str, *, stop_reason: str | None = None) -> None:
        self._apply(state, GoalPhase.PAUSED, reason, stop_reason=stop_reason)

    def _cancel(self, state: GoalState, detail: str) -> None:
        state.last_error = detail
        self._apply(state, GoalPhase.CANCELLED, StopReason.cancelled_by_user.value, error=detail, stop_reason=StopReason.cancelled_by_user.value)

    def _fail(self, state: GoalState, reason: StopReason | str, detail: str) -> None:
        state.last_error = detail
        value = reason.value if isinstance(reason, StopReason) else str(reason)
        self._apply(state, GoalPhase.FAILED, value, error=detail, stop_reason=value)
