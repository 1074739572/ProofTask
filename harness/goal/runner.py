"""Background Goal runner using Task-owned contracts and evidence."""

from __future__ import annotations

import json
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
from harness.project.resume import checkpoint_history
from harness.settings import get_workdir, workspace_generation
from harness.verification import build_pytest_command, collect_pytest_catalog, reverify_task_command, run_verification, verify_task_command

ACT_DISABLED_TOOLS = frozenset({"complete_task", "clear_tasks"})


class GoalNotRunningError(Exception):
    pass


class GoalBusyError(Exception):
    pass


@dataclass(frozen=True)
class GoalRequest:
    target: str
    verification: str
    max_rounds_per_attempt: int = 20
    max_attempts: int = 3
    max_consecutive_failures: int = 3
    max_duration_seconds: int = 1800
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
        problems = validate_limits(state)
        if problems:
            raise ValueError("invalid goal limits: " + "; ".join(problems))
        save_goal(state)
        _runner = GoalRunner(state=state, history=history, context=context, binding=binding)
        _runner.start()
        _emit("goal_started", id=state.id, phase=state.phase, status=state.status)
        return state


def resume_goal(*, history: list, context: dict, binding: Any) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        state = load_goal()
        if state is None or state.status != GoalStatus.PAUSED.value:
            raise GoalNotRunningError("No paused goal to resume.")
        if state.stop_reason == StopReason.test_generation_required.value:
            state.phase = GoalPhase.PREPARE_TESTS.value
            state.status = GoalStatus.RUNNING.value
            state.stop_reason = None
        elif state.phase == GoalPhase.PREPARE_TESTS.value:
            state.phase = GoalPhase.SELECT_TASK.value
            state.status = GoalStatus.RUNNING.value
            state.stop_reason = None
        else:
            GoalEngine().transition(state, GoalPhase.SELECT_TASK, "resume")
        save_goal(state)
        _runner = GoalRunner(state=state, history=history, context=context, binding=binding)
        _runner.start()
        return state


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
        return self._state

    def run(self) -> None:
        try:
            self._drive()
        except Exception as exc:
            self._fail(self._state, StopReason.internal_error, f"{type(exc).__name__}: {exc}")
        finally:
            if self._state.status in {GoalStatus.DONE.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value}:
                archive_goal(self._state)

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
        elif state.phase == GoalPhase.FULL_VERIFY.value:
            self._full_verify(state)
        elif state.phase == GoalPhase.CLEAN_CHECK.value:
            self._clean_check(state)
        else:
            raise GoalNotRunningError(f"cannot step from {state.phase}")

    def _initialize(self, state: GoalState) -> None:
        from harness.tasks import create_task

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
        if not state.task_ids:
            names: dict[str, str] = {}
            for plan in plans:
                deps = [names[name] for name in plan.get("depends_on", [])]
                task = create_task(
                    plan["name"], plan["behavior"], deps, goal_id=state.id,
                    acceptance_cases=list(plan.get("acceptance_cases") or []),
                    verification_spec=dict(plan.get("verification_spec") or {}),
                    evaluation_required=state.evaluation_required,
                )
                names[plan["name"]] = task.id
                state.task_ids.append(task.id)
                save_goal(state)
        state.initialization_complete = True
        state.current_task_id = state.task_ids[0] if state.task_ids else None
        save_goal(state)
        if self._needs_test_generation(state):
            self._apply(state, GoalPhase.PREPARE_TESTS, "task_tests_required")
        else:
            self._apply(state, GoalPhase.SELECT_TASK, "initialize_complete")

    def _needs_test_generation(self, state: GoalState) -> bool:
        from harness.tasks import load_task

        return any(load_task(task_id).verification_state == "needs_generation" for task_id in state.task_ids)

    def _prepare_tests(self, state: GoalState) -> None:
        """Generate and bind tests before any implementation Task is selected."""
        from harness.tasks import bind_task_verification, load_task

        root = Path(state.workspace)
        for task_id in state.task_ids:
            task = load_task(task_id)
            if task.verification_state != "needs_generation":
                continue
            prompt = (
                "Create focused pytest coverage for this Task before implementation. Do not change production code. "
                "Use existing test conventions. After writing tests, reply ONLY with JSON: "
                '{"test_selectors":["tests/test_x.py::test_name"]}.\n\n'
                f"Task: {task.subject}\nBehavior: {task.description}\nAcceptance cases: {json.dumps(task.acceptance_cases)}"
            )
            stats = AgentTaskStats()
            self._phase_in_flight = True
            try:
                raw = run_agent_task(description=f"generate tests for task {task.id}", prompt=prompt, agent_type="goal_test_writer", cwd=str(root), max_rounds=PLANNER_MAX_ROUNDS, cancel_check=self._interrupted, deadline=self._deadline(state), stats=stats)
            finally:
                self._phase_in_flight = False
            state.total_llm_rounds += stats.llm_rounds
            selectors = self._selectors_from_generation(raw, root)
            if not selectors:
                self._pause(state, "test_generation_required", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} needs a collected pytest selector before execution."
                save_goal(state)
                return
            command = build_pytest_command(selectors)
            baseline = run_verification(command, workspace=root)
            if baseline.error or baseline.timed_out or baseline.passed:
                detail = baseline.error or (
                    "generated tests passed before implementation; they do not prove the missing behavior"
                    if baseline.passed
                    else "generated test baseline timed out"
                )
                self._pause(state, "test_generation_baseline_failed", stop_reason=StopReason.test_generation_required.value)
                state.last_error = f"Task {task.id} test baseline is invalid: {detail}"
                save_goal(state)
                return
            bind_task_verification(task.id, VerificationSpec(adapter="pytest", command=command, test_files=tuple(item.split("::", 1)[0] for item in selectors), selectors=tuple(selectors), source="generated", collected_count=len(selectors), baseline_result="failing", confidence="high").to_dict())
        self._apply(state, GoalPhase.SELECT_TASK, "task_tests_bound")

    @staticmethod
    def _selectors_from_generation(raw: str, workspace: Path) -> tuple[str, ...]:
        try:
            data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            requested = tuple(str(item) for item in data.get("test_selectors", []))
        except (ValueError, TypeError, json.JSONDecodeError):
            return ()
        catalog = collect_pytest_catalog(workspace)
        return requested if requested and all(catalog.contains(item) for item in requested) else ()

    def _select_task(self, state: GoalState) -> None:
        from harness.tasks import load_task

        for task_id in state.task_ids:
            task = load_task(task_id)
            if task.status == "completed":
                continue
            if not all(load_task(dep).status == "completed" for dep in task.blockedBy):
                continue
            state.current_task_id = task_id
            state.attempts = 0
            state.no_progress_count = 0
            save_goal(state)
            self._apply(state, GoalPhase.ACT if task.status == "in_progress" else GoalPhase.CLAIM, "task_selected")
            return
        self._apply(state, GoalPhase.FULL_VERIFY, "all_tasks_completed")

    def _claim(self, state: GoalState) -> None:
        from harness.tasks import claim_task

        claim_task(state.current_task_id, owner=f"goal:{state.id}")
        self._apply(state, GoalPhase.ACT, "task_claimed")

    def _act(self, state: GoalState) -> None:
        from harness.goal.prompt import build_goal_act_prompt
        from harness.tasks import load_task

        task = load_task(state.current_task_id)
        before = self._progress_snapshot(state)
        stats = LoopStats()
        self._phase_in_flight = True
        try:
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            with agent_lock:
                clear_cancel()
                self._history.append({"role": "user", "content": build_goal_act_prompt(state, task)})
                agent_loop(self._history, self._context, max_rounds=state.max_rounds_per_attempt, binding=self._binding, disabled_tools=ACT_DISABLED_TOOLS, stats=stats)
        finally:
            set_goal_noninteractive(False)
            self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        state.attempts += 1
        state.no_progress_count = state.no_progress_count + 1 if self._progress_snapshot(state) == before else 0
        if self._binding is not None:
            checkpoint_history(self._history, binding=self._binding)
        if self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
        elif self._pause_event.is_set() or goal_permission_pending():
            self._pause(state, "permission_wait" if goal_permission_pending() else "user_pause", stop_reason=StopReason.permission_wait.value if goal_permission_pending() else None)
        else:
            self._apply(state, GoalPhase.VERIFY, "agent_loop_finished")

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
        run_task_evaluation(
            state.current_task_id,
            state.workspace,
            cancel_check=self._interrupted,
            deadline=self._deadline(state),
        )
        self._apply(state, GoalPhase.CLEAN_CHECK, "evaluation_recorded")

    def _clean_check(self, state: GoalState) -> None:
        from harness.tasks import complete_task

        result = complete_task(state.current_task_id, clean_check_mode="enforce")
        if result.startswith("Completed") or "already completed" in result:
            self._apply(state, GoalPhase.SELECT_TASK, "task_completed")
            return
        state.consecutive_failures += 1
        self._apply(state, GoalPhase.ACT, "clean_check_failed", error=result)

    def _full_verify(self, state: GoalState) -> None:
        for task_id in state.task_ids:
            task = reverify_task_command(task_id, workspace=state.workspace)
            if task.verification_state != "passing":
                self._fail(
                    state,
                    StopReason.full_verification_failed,
                    f"Task {task_id} final binding failed: {task.last_error}",
                )
                return
        result = run_verification(state.verification, workspace=Path(state.workspace))
        if result.passed:
            self._apply(state, GoalPhase.DONE, "goal_verification_passed")
        else:
            self._fail(state, StopReason.full_verification_failed, result.error or f"full verification failed with exit code {result.exit_code}")

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
        _emit("goal_phase", id=state.id, phase=state.phase, status=state.status, task_id=state.current_task_id, attempt=state.attempts)

    def _pause(self, state: GoalState, reason: str, *, stop_reason: str | None = None) -> None:
        self._apply(state, GoalPhase.PAUSED, reason, stop_reason=stop_reason)

    def _cancel(self, state: GoalState, detail: str) -> None:
        state.last_error = detail
        self._apply(state, GoalPhase.CANCELLED, StopReason.cancelled_by_user.value, error=detail, stop_reason=StopReason.cancelled_by_user.value)

    def _fail(self, state: GoalState, reason: StopReason | str, detail: str) -> None:
        state.last_error = detail
        value = reason.value if isinstance(reason, StopReason) else str(reason)
        self._apply(state, GoalPhase.FAILED, value, error=detail, stop_reason=value)
