"""Background Goal runner using Task-owned contracts and evidence."""

from __future__ import annotations

import json
import hashlib
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
from harness.goal.models import GoalPhase, GoalStatus, GoalState, StopReason
from harness.goal.planner import PLANNER_MAX_ROUNDS, VerificationSpec, plan_tasks
from harness.goal.policy import check_stop, validate_limits
from harness.goal.store import GoalStoreError, archive_goal, load_goal, save_goal
from harness.loop import LoopStats, agent_lock, agent_loop
from harness.settings import get_workdir, workspace_generation
from harness.verification import build_pytest_command, collect_pytest_catalog, reverify_task_command, run_verification, verify_task_command
from harness.verification.evidence import evidence_from_result

ACT_DISABLED_TOOLS = frozenset({"complete_task", "clear_tasks"})


class GoalNotRunningError(Exception):
    pass


class GoalBusyError(Exception):
    pass


@dataclass(frozen=True)
class GoalRequest:
    target: str
    verification: str
    # A user-approved draft can seed the durable Task plan. The runner still
    # owns test generation, baselines, implementation, and every completion gate.
    task_plan: list[dict[str, Any]] | None = None
    goal_contract: dict[str, Any] | None = None
    await_execution_approval: bool = False
    max_rounds_per_attempt: int = 20
    max_attempts: int = 3
    max_consecutive_failures: int = 3
    max_duration_seconds: int = 1800
    max_repair_attempts: int = 2
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
        "current_task_id": state.current_task_id,
        "attempts": state.attempts,
        "max_attempts": state.max_attempts,
        "total_llm_rounds": state.total_llm_rounds,
        "max_total_rounds": state.max_total_rounds,
        "stop_reason": state.stop_reason,
        "final_verification": dict(state.final_verification) if isinstance(state.final_verification, dict) else None,
        "last_error": state.last_error,
        "tasks": tasks,
    }


def _emit_goal(event_type: str, state: GoalState) -> None:
    _emit(event_type, **goal_event_payload(state))


def emit_current_goal_status(*, include_terminal: bool = True) -> GoalState | None:
    """Emit the persisted Goal snapshot for TUI hydration and `/goal status`."""
    try:
        state = load_goal()
    except GoalStoreError as exc:
        _emit("goal_status_error", code=exc.code, error=str(exc))
        return None
    if state is None:
        return None
    terminal = {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}
    if include_terminal or state.status not in terminal:
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
        f"  Attempt: {state.attempts}/{state.max_attempts}",
        f"  Elapsed: {int(max(0, time.time() - state.started_at))}s / {state.max_duration_seconds}s",
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
        state = GoalState.new(
            target=request.target, verification=request.verification, workspace=str(get_workdir()),
            workspace_generation=workspace_generation(), evaluation_required=request.evaluation_required,
            max_rounds_per_attempt=request.max_rounds_per_attempt, max_attempts=request.max_attempts,
            max_consecutive_failures=request.max_consecutive_failures, max_duration_seconds=request.max_duration_seconds,
        )
        state.max_repair_attempts = request.max_repair_attempts
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
        save_goal(state)
        _runner = GoalRunner(state=state, history=history, context=context, binding=binding)
        _emit_goal("goal_started", state)
        _runner.start()
        return state


def resume_goal(*, history: list, context: dict, binding: Any, approve_execution: bool = False) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        state = load_goal()
        if state is None or state.status != GoalStatus.PAUSED.value:
            raise GoalNotRunningError("No paused goal to resume.")
        if state.stop_reason == StopReason.user_approval_required.value:
            if not approve_execution:
                raise GoalNotRunningError("Goal is waiting for /goal run. Ordinary /goal resume cannot approve implementation.")
            state.execution_approved = True
        elif approve_execution:
            raise GoalNotRunningError("No Goal is waiting for execution approval.")

        # Pauses are outside the active execution budget. This also makes a
        # draft safely resumable the next day after test review.
        if state.paused_at is not None:
            state.started_at += max(0.0, time.time() - state.paused_at)
            state.paused_at = None

        target = _resume_target(state)
        state.phase = target
        state.status = GoalStatus.RUNNING.value
        state.stop_reason = None
        save_goal(state)
        _runner = GoalRunner(state=state, history=history, context=context, binding=binding)
        _emit_goal("goal_started", state)
        _runner.start()
        return state


def _resume_target(state: GoalState) -> str:
    """Recover only to a phase whose durable prerequisites still hold."""
    from harness.tasks import load_task

    if not state.initialization_complete or len(state.task_name_ids) < len(state.task_plan):
        return GoalPhase.INITIALIZE.value
    tasks = []
    try:
        tasks = [load_task(task_id) for task_id in state.task_ids]
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return GoalPhase.INITIALIZE.value
    if any(task.verification_state == "needs_generation" for task in tasks):
        return GoalPhase.PREPARE_TESTS.value
    if not state.execution_approved:
        return GoalPhase.PREPARE_TESTS.value
    candidate = state.resume_phase or GoalPhase.SELECT_TASK.value
    allowed = {
        phase.value for phase in (
            GoalPhase.SELECT_TASK, GoalPhase.CLAIM, GoalPhase.ACT,
            GoalPhase.VERIFY, GoalPhase.EVALUATE, GoalPhase.REPAIR_PLAN,
            GoalPhase.IMPACT_REVIEW, GoalPhase.CLEAN_CHECK, GoalPhase.FULL_VERIFY,
        )
    }
    return candidate if candidate in allowed else GoalPhase.SELECT_TASK.value


def pause_goal() -> GoalState:
    with _runner_lock:
        if _runner is None:
            raise GoalNotRunningError("Goal is not running.")
        return _runner.request_pause()


def cancel_goal() -> GoalState:
    with _runner_lock:
        if _runner is None:
            raise GoalNotRunningError("Goal is not running.")
        return _runner.request_cancel()


class GoalRunner(threading.Thread):
    def __init__(self, *, state: GoalState, history: list, context: dict, binding: Any):
        super().__init__(name=f"goal-{state.id}", daemon=True)
        self._state, self._history, self._context, self._binding = state, history, context, binding
        self._lock = threading.RLock()
        self._pause_event, self._cancel_event = threading.Event(), threading.Event()
        self._phase_in_flight = False
        self._archived = False
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
            if self._state.status in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                archive_goal(self._state)
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
            decision = check_stop(state, now=time.time())
            if decision.stop and state.phase not in {GoalPhase.VERIFY.value, GoalPhase.FULL_VERIFY.value}:
                self._fail(state, decision.reason or StopReason.internal_error, decision.detail)
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
        from harness.tasks import create_task, load_task, save_task

        root = Path(state.workspace)
        if not state.task_plan:
            stats = AgentTaskStats()
            self._phase_in_flight = True
            try:
                plans = plan_tasks(state.target, state.verification, root, cancel_check=self._interrupted, deadline=self._deadline(state), stats=stats)
            finally:
                self._phase_in_flight = False
            state.total_llm_rounds += stats.llm_rounds
            state.task_plan = [item.to_dict() for item in plans]
            save_goal(state)
        plans = state.task_plan
        # Task creation is deliberately idempotent. A process may stop after
        # writing one Task but before writing the next Goal checkpoint.
        names = dict(state.task_name_ids)
        for task_id in state.task_ids:
            try:
                existing = load_task(task_id)
            except (FileNotFoundError, OSError, TypeError, ValueError):
                continue
            if existing.subject in {str(plan.get("name")) for plan in plans}:
                names.setdefault(existing.subject, existing.id)
        for plan in plans:
            name = str(plan["name"])
            if name in names:
                continue
            deps = [names[dep] for dep in plan.get("depends_on", [])]
            task = create_task(
                name, plan["behavior"], deps, goal_id=state.id,
                acceptance_cases=list(plan.get("acceptance_cases") or []),
                verification_spec=dict(plan.get("verification_spec") or {}),
                evaluation_required=state.evaluation_required,
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
            spec["covers"] = list(spec.get("covers") or [
                str(case.get("id"))
                for case in task.acceptance_cases
                if isinstance(case, dict) and case.get("id")
            ])
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

        root = Path(state.workspace)
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
            before_catalog = collect_pytest_catalog(root)
            before_hashes = self._test_file_hashes(root, before_catalog.test_files)
            prompt = (
                "Create a NEW focused pytest file for this Task before implementation. You may modify only test files; "
                "do not edit existing test files or production code. Use existing test conventions. After writing tests, reply ONLY with JSON: "
                '{"test_selectors":["tests/test_x.py::test_name"]}.\n\n'
                f"Task: {task.subject}\nBehavior: {task.description}\nAcceptance cases: {json.dumps(task.acceptance_cases)}"
            )
            stats = AgentTaskStats()
            self._phase_in_flight = True
            try:
                raw = run_agent_task(
                    description=f"generate tests for task {task.id}",
                    prompt=prompt,
                    agent_type="goal_test_writer",
                    cwd=str(root),
                    max_rounds=PLANNER_MAX_ROUNDS,
                    cancel_check=self._interrupted,
                    deadline=self._deadline(state),
                    stats=stats,
                    write_roots=self._test_write_roots(before_catalog),
                )
            finally:
                self._phase_in_flight = False
            state.total_llm_rounds += stats.llm_rounds
            after_catalog = collect_pytest_catalog(root)
            selectors = self._selectors_from_generation(raw, root, catalog=after_catalog)
            if not selectors:
                self._pause(state, "test_generation_required", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} needs a collected pytest selector before execution."
                save_goal(state)
                return
            if any(selector in before_catalog.selectors for selector in selectors):
                self._pause(state, "test_generation_reused_existing_selector", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test writer reused an existing selector; it must add focused coverage."
                save_goal(state)
                return
            changed_existing = [
                path for path, digest in before_hashes.items()
                if self._test_file_hashes(root, (path,)).get(path) != digest
            ]
            if changed_existing:
                self._pause(state, "test_generation_changed_existing_test", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test writer modified existing test files: {', '.join(changed_existing)}"
                save_goal(state)
                return
            command = build_pytest_command(selectors)
            baseline = run_verification(command, workspace=root)
            output = str(getattr(baseline, "stdout", "") or "").lower()
            infrastructure_failure = "error collecting" in output or "importerror" in output or "fixture" in output and "error" in output
            posthoc = bool(task.verification_spec.get("allow_posthoc_test"))
            if baseline.error or baseline.timed_out or (baseline.passed and not posthoc) or infrastructure_failure:
                detail = baseline.error or (
                    "generated tests passed before implementation; they do not prove the missing behavior"
                    if baseline.passed
                    else "generated test baseline failed outside the requested behavior"
                )
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
            bound = bind_task_verification(
                task.id,
                VerificationSpec(
                    adapter="pytest",
                    command=build_pytest_command(all_selectors),
                    test_files=files,
                    selectors=all_selectors,
                    source="generated",
                    collected_count=len(selectors),
                    baseline_result="posthoc_passing" if baseline.passed else "failing",
                    confidence="high",
                    baseline_evidence=baseline_evidence,
                test_hashes=self._test_file_hashes(root, files),
                covers=tuple(str(case.get("id")) for case in task.acceptance_cases if isinstance(case, dict) and case.get("id")),
                owners=tuple(
                    dict.fromkeys(
                        str(owner)
                        for owner in task.verification_spec.get("owners", [task.id])
                        if owner
                    )
                ),
            ).to_dict(),
            )
            record_test_binding(
                state,
                bound,
                bound.verification_spec,
                kind="integration" if len(bound.verification_spec.get("owners") or []) > 1 else "task",
            )
        if not state.execution_approved:
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
        return requested if requested and all(catalog.contains(item) for item in requested) else ()

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
    def _test_write_roots(catalog) -> tuple[str, ...]:
        roots = {"tests", "test", "__tests__"}
        for test_file in getattr(catalog, "test_files", ()):
            parent = str(Path(test_file).parent).replace("\\", "/")
            if parent and parent != ".":
                roots.add(parent)
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
        from harness.tasks import claim_task, record_task_start
        from harness.verification.snapshot import capture_code_snapshot

        claim_task(state.current_task_id, owner=f"goal:{state.id}")
        record_task_start(
            state.current_task_id,
            snapshot=capture_code_snapshot(state.workspace),
            diff=self._workspace_diff(state.workspace),
        )
        self._apply(state, GoalPhase.ACT, "task_claimed")

    def _act(self, state: GoalState) -> None:
        from harness.goal.memory import write_handoff
        from harness.goal.prompt import build_goal_act_prompt
        from harness.tasks import load_task

        task = load_task(state.current_task_id)
        before = self._progress_snapshot(state)
        stats = LoopStats()
        worker_history = [{"role": "user", "content": build_goal_act_prompt(state, task)}]
        worker_context = self._goal_worker_context()
        write_handoff(state, task, phase=GoalPhase.ACT.value)
        self._phase_in_flight = True
        try:
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            with agent_lock:
                clear_cancel()
                agent_loop(worker_history, worker_context, max_rounds=state.max_rounds_per_attempt, binding=None, disabled_tools=ACT_DISABLED_TOOLS, stats=stats)
        finally:
            set_goal_noninteractive(False)
            self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        state.attempts += 1
        state.no_progress_count = state.no_progress_count + 1 if self._progress_snapshot(state) == before else 0
        write_handoff(state, task, phase=GoalPhase.VERIFY.value, summary=self._worker_summary(worker_history))
        if self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
        elif goal_permission_pending():
            self._fail(
                state,
                StopReason.autonomy_blocked,
                "a tool required permission outside the approved Goal boundary",
            )
        elif self._pause_event.is_set():
            self._pause(state, "user_pause")
        else:
            self._apply(state, GoalPhase.VERIFY, "agent_loop_finished")

    def _goal_worker_context(self) -> dict[str, Any]:
        """Keep project rules, never the user's stale chat state or Todo list."""
        allowed = {"project_instructions", "memories", "connected_mcp"}
        return {key: value for key, value in self._context.items() if key in allowed}

    @staticmethod
    def _worker_summary(history: list[dict]) -> str:
        from harness.tools.dispatch import extract_text

        for message in reversed(history):
            if message.get("role") == "assistant":
                text = extract_text(message.get("content"))
                if text:
                    return text[:4_000]
        return ""

    def _verify(self, state: GoalState) -> None:
        task = verify_task_command(state.current_task_id, workspace=state.workspace)
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
            state.workspace,
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        evaluation = task.evaluation or {}
        if evaluation.get("passed") is True:
            self._apply(state, GoalPhase.CLEAN_CHECK, "evaluation_passed")
            return
        state.last_error = str(evaluation.get("error") or evaluation.get("summary") or "evaluator rejected Task")
        self._apply(state, GoalPhase.REPAIR_PLAN, "evaluation_requires_repair", error=state.last_error)

    def _repair_plan(self, state: GoalState) -> None:
        from harness.goal.memory import append_decisions
        from harness.goal.repair import plan_task_repair
        from harness.tasks import load_task, record_task_repair, request_task_test_repair

        if state.repair_attempts >= state.max_repair_attempts:
            self._fail(state, StopReason.repair_budget_exhausted, "reached autonomous repair budget")
            return
        task = load_task(state.current_task_id)
        evaluation = task.evaluation or {"passed": False, "summary": state.last_error or "Goal verification failed", "route": "implementation_fix"}
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
            cwd=state.workspace,
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        record = {"evaluation": evaluation, **decision.to_dict(), "at": time.time()}
        record_task_repair(task.id, record)
        if decision.assumptions:
            append_decisions(state, task, list(decision.assumptions), source="repair_planner")
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
        from harness.tasks import complete_task

        result = complete_task(state.current_task_id, clean_check_mode="enforce")
        if result.startswith("Completed") or "already completed" in result:
            self._apply(state, GoalPhase.IMPACT_REVIEW, "task_completed")
            return
        state.consecutive_failures += 1
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
            cwd=state.workspace,
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
            stats=stats,
        )
        state.total_llm_rounds += stats.llm_rounds
        if decision.action == "add_tests" and decision.task_id:
            target = request_task_test_repair(decision.task_id)
            target.verification_spec["owners"] = list(dict.fromkeys((completed.id, target.id)))
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
            task = reverify_task_command(task_id, workspace=state.workspace)
            if task.verification_state != "passing":
                self._record_final_verification(
                    state,
                    status="blocked",
                    error=f"Task {task_id} final binding failed: {task.last_error}",
                )
                self._queue_goal_repair(
                    state,
                    f"Task {task_id} final binding failed: {task.last_error}",
                )
                return
        state.final_verification = {
            "status": "running",
            "command": state.verification,
            "updated_at": time.time(),
        }
        save_goal(state)
        _emit_goal("goal_status", state)
        result = run_verification(state.verification, workspace=Path(state.workspace))
        self._record_final_verification(state, status="passed" if result.passed else "failed", result=result)
        if result.passed:
            self._apply(state, GoalPhase.DONE, "goal_verification_passed")
        else:
            self._queue_goal_repair(
                state,
                result.error or f"full verification failed with exit code {result.exit_code}",
            )

    def _queue_goal_repair(self, state: GoalState, detail: str) -> None:
        """Turn a final regression failure into a durable corrective Task."""
        from harness.tasks import create_task, record_task_evaluation

        if state.repair_attempts >= state.max_repair_attempts:
            self._fail(state, StopReason.full_verification_failed, detail)
            return
        name = f"goal regression repair {state.repair_attempts + 1}"
        task = create_task(
            name,
            f"Repair the Goal-level regression failure: {detail}",
            goal_id=state.id,
            acceptance_cases=[
                {
                    "id": "GOAL_REGRESSION",
                    "given": "all previously completed Goal Tasks",
                    "when": "the configured full verification runs",
                    "then": "the full verification command exits with code 0",
                }
            ],
            verification_spec={"source": "needs_generation"},
            evaluation_required=True,
        )
        record_task_evaluation(
            task.id,
            {
                "passed": False,
                "route": "replan",
                "summary": detail,
                "findings": [{"issue": detail, "severity": "high", "evidence": "full verification"}],
            },
        )
        state.task_ids.append(task.id)
        state.task_name_ids[name] = task.id
        state.task_plan.append(
            {
                "name": name,
                "behavior": task.description,
                "depends_on": [],
                "acceptance_cases": task.acceptance_cases,
                "verification_spec": task.verification_spec,
            }
        )
        state.current_task_id = task.id
        state.last_error = detail
        save_goal(state)
        self._apply(state, GoalPhase.REPAIR_PLAN, "goal_regression_requires_repair", error=detail)

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
                workspace=state.workspace,
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
        return (task.evidence, task.last_error, capture_code_snapshot(state.workspace))

    def _interrupted(self) -> bool:
        return self._cancel_event.is_set() or self._pause_event.is_set()

    def _deadline(self, state: GoalState) -> float:
        return time.monotonic() + max(0, state.max_duration_seconds - (time.time() - state.started_at))

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
