"""Goal runner: background orchestration of L2–L5 (L6 /goal).

Module-level API (used by commands.py and the CLI/TUI):

    start_goal(request, *, history, context, binding) -> GoalState
    resume_goal(*, history, context, binding) -> GoalState
    pause_goal() -> GoalState
    cancel_goal() -> GoalState
    get_goal_status() -> str
    is_goal_running() -> bool

Concurrency model (docs/goal-mode-mvp-spec.md §7.5):

- at most one goal runner thread per process;
- ``threading.RLock`` + Events manage pause/cancel;
- the ACT phase runs inside ``agent_lock`` so it never overlaps a normal turn;
- ``harness.agent.cancel`` only cancels the current ``agent_loop`` round;
- goal ACTs are non-interactive: any tool ``ask`` is returned as a rejection
  and the goal pauses with ``stop_reason=permission_wait`` (thread-local flag,
  never a process-global).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.agent.cancel import clear_cancel, request_cancel
from harness.clean import run_clean_check
from harness.evaluation import run_evaluation
from harness.goal.engine import GoalEngine
from harness.goal.models import (
    GoalPhase,
    GoalStatus,
    GoalState,
    StopReason,
)
from harness.goal.policy import check_stop
from harness.goal.store import (
    GoalStoreError,
    archive_goal,
    load_goal,
    save_goal,
)
from harness.loop import LoopStats, agent_lock, agent_loop
from harness.settings import get_workdir, workspace_generation
from harness.verification import verify_feature_command

#: Tools hidden from the agent during ACT — the model cannot bypass the runner
#: or mutate orchestration state (spec §7.5).
ACT_DISABLED_TOOLS = frozenset(
    {
        "create_feature",
        "claim_feature",
        "verify_feature",
        "evaluate_feature",
        "complete_task",
        "clear_tasks",
    }
)


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
    evaluation_required: bool = False


# --- thread-local non-interactive permission context -------------------------

_local = threading.local()


def is_goal_noninteractive() -> bool:
    """True on the runner thread while an ACT phase is executing."""
    return bool(getattr(_local, "noninteractive", False))


def goal_permission_pending() -> bool:
    """True when a tool asked for permission during a goal ACT."""
    return bool(getattr(_local, "permission_pending", False))


def mark_goal_permission_pending() -> None:
    _local.permission_pending = True


def clear_goal_permission_flags() -> None:
    _local.permission_pending = False
    _local.noninteractive = False


def set_goal_noninteractive(active: bool) -> None:
    _local.noninteractive = active


# --- status formatting -------------------------------------------------------

def format_goal_status(state: GoalState) -> str:
    now = time.time()
    elapsed = max(0.0, now - state.started_at)
    lines = [
        f"Goal {state.id} [{state.phase}] ({state.status})",
        f"  Target: {state.target[:80]}",
        f"  Feature: {state.feature_id or '-'}",
        f"  Attempt: {state.attempts}/{state.max_attempts}",
        f"  Elapsed: {int(elapsed)}s / {state.max_duration_seconds}s",
        f"  Consecutive failures: {state.consecutive_failures}/{state.max_consecutive_failures}",
    ]
    if state.last_phase and state.last_phase != state.phase:
        lines.append(f"  Last transition: {state.last_phase} -> {state.phase}")
    if state.last_error:
        lines.append(f"  Last error: {str(state.last_error)[:200]}")
    if state.stop_reason:
        lines.append(f"  Stop reason: {state.stop_reason}")
    return "\n".join(lines)


def get_goal_status() -> str:
    # Live runner in this process? Show its in-memory state — load_goal()
    # normalizes a running goal to paused for cross-process restart recovery,
    # which must NOT make a running goal look paused here.
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            with runner._lock:
                return format_goal_status(runner._state)
    try:
        state = load_goal()
    except GoalStoreError as exc:
        return f"Goal state is {exc.code}: {exc}"
    if state is None:
        return (
            "No goal in this workspace. Start one with:\n"
            '  /goal --verify "<command>" -- <target>'
        )
    return format_goal_status(state)


# --- module-level runner handle ----------------------------------------------

_runner: "GoalRunner | None" = None
_runner_lock = threading.Lock()


def _reap_runner() -> None:
    """Drop a finished runner so a new goal can start."""
    global _runner
    with _runner_lock:
        if _runner is not None and (not _runner.is_alive() or not _runner.is_running()):
            _runner = None


def is_goal_running() -> bool:
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            return True
    try:
        state = load_goal()
    except GoalStoreError:
        return False
    return state is not None and state.status in (
        GoalStatus.RUNNING.value,
        GoalStatus.PAUSING.value,
    )


def start_goal(
    request: GoalRequest,
    *,
    history: list,
    context: dict,
    binding: Any,
) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            raise GoalBusyError(
                "A goal is already running. Use /goal status|pause|cancel first."
            )
        state = GoalState.new(
            target=request.target,
            verification=request.verification,
            workspace=str(get_workdir()),
            workspace_generation=workspace_generation(),
            evaluation_required=request.evaluation_required,
            max_rounds_per_attempt=request.max_rounds_per_attempt,
            max_attempts=request.max_attempts,
            max_consecutive_failures=request.max_consecutive_failures,
            max_duration_seconds=request.max_duration_seconds,
        )
        save_goal(state)
        _emit("goal_started", id=state.id, phase=state.phase, status=state.status)
        runner = GoalRunner(
            state=state,
            history=history,
            context=context,
            binding=binding,
        )
        runner.start()
        _runner = runner
        return state


def resume_goal(*, history: list, context: dict, binding: Any) -> GoalState:
    global _runner
    _reap_runner()
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            raise GoalBusyError(
                "A goal is already running. Use /goal status|pause|cancel first."
            )
        state = load_goal()
        if state is None:
            raise GoalNotRunningError("No goal to resume in this workspace.")
        if state.status in (
            GoalStatus.DONE.value,
            GoalStatus.FAILED.value,
            GoalStatus.CANCELLED.value,
        ):
            raise GoalNotRunningError(
                f"Goal {state.id} already {state.status} — start a new goal."
            )
        try:
            if (
                Path(state.workspace).expanduser().resolve()
                != get_workdir().expanduser().resolve()
            ):
                raise GoalNotRunningError(
                    "Goal belongs to a different workspace — /open it first."
                )
        except OSError:
            pass
        engine = GoalEngine()
        state = engine.transition(state, GoalPhase.SELECT_FEATURE, "resumed")
        save_goal(state)
        _emit("goal_status", id=state.id, phase=state.phase, status=state.status)
        runner = GoalRunner(
            state=state,
            history=history,
            context=context,
            binding=binding,
        )
        runner.start()
        _runner = runner
        return state


def pause_goal() -> GoalState:
    with _runner_lock:
        runner = _runner
        if runner is None or not (runner.is_alive() and runner.is_running()):
            raise GoalNotRunningError("No goal is running to pause.")
        return runner.request_pause()


def cancel_goal() -> GoalState:
    _reap_runner()
    with _runner_lock:
        runner = _runner
        if runner is not None and runner.is_alive() and runner.is_running():
            return runner.request_cancel()
        state = load_goal()
        if state is None:
            raise GoalNotRunningError("No goal in this workspace to cancel.")
        if state.status in (
            GoalStatus.DONE.value,
            GoalStatus.FAILED.value,
            GoalStatus.CANCELLED.value,
        ):
            raise GoalNotRunningError(f"Goal {state.id} already {state.status}.")
        engine = GoalEngine()
        state = engine.transition(
            state,
            GoalPhase.CANCELLED,
            StopReason.cancelled_by_user.value,
            stop_reason=StopReason.cancelled_by_user.value,
        )
        save_goal(state)
        archive_goal(state)
        _emit("goal_stopped", id=state.id, status=state.status, stop_reason=state.stop_reason)
        return state


def _emit(event_type: str, **payload: Any) -> None:
    from harness.ui import events

    if events.is_enabled():
        events.emit(event_type, **payload)


class GoalRunner(threading.Thread):
    """Background orchestration thread.

    Tests may call :meth:`run` directly (synchronously) with mocked
    agent/verification/evaluation/clean calls.
    """

    def __init__(
        self,
        *,
        state: GoalState,
        history: list,
        context: dict,
        binding: Any,
    ):
        super().__init__(name=f"goal-{state.id}", daemon=True)
        self._state = state
        self._history = history
        self._context = context
        self._binding = binding
        self._lock = threading.RLock()
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._act_in_flight = False
        self._archived = False
        self.engine = GoalEngine()

    # --- public control (called from other threads) -------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._state.status in (
                GoalStatus.RUNNING.value,
                GoalStatus.PAUSING.value,
            )

    def request_pause(self) -> GoalState:
        with self._lock:
            state = self._state
            if state.status == GoalStatus.PAUSING.value:
                return state
            if state.status != GoalStatus.RUNNING.value:
                raise GoalNotRunningError("Goal is not running.")
            state.status = GoalStatus.PAUSING.value
            save_goal(state)
            self._pause_event.set()
            in_flight = self._act_in_flight
        if in_flight:
            from harness.agent.cancel import request_cancel

            request_cancel()
        return state

    def request_cancel(self) -> GoalState:
        with self._lock:
            state = self._state
            if state.status == GoalStatus.CANCELLED.value:
                return state
            if state.status not in (
                GoalStatus.RUNNING.value,
                GoalStatus.PAUSING.value,
            ):
                raise GoalNotRunningError("Goal is not running.")
            self._cancel_event.set()
            in_flight = self._act_in_flight
        if in_flight:
            from harness.agent.cancel import request_cancel

            request_cancel()
        return state

    # --- main loop ----------------------------------------------------------

    def run(self) -> None:
        try:
            self._drive()
        except Exception as exc:  # emergency fuse: never leave a stuck goal
            try:
                with self._lock:
                    state = self._state
                    if state.status not in (
                        GoalStatus.DONE.value,
                        GoalStatus.FAILED.value,
                        GoalStatus.CANCELLED.value,
                    ):
                        self._fail(
                            state,
                            StopReason.internal_error,
                            f"{type(exc).__name__}: {exc}",
                        )
            except Exception:
                pass
        finally:
            with self._lock:
                self._act_in_flight = False
            self._archive_terminal()

    def _drive(self) -> None:
        while True:
            with self._lock:
                state = self._state
                if state is None:
                    return
                if state.status in (
                    GoalStatus.DONE.value,
                    GoalStatus.FAILED.value,
                    GoalStatus.CANCELLED.value,
                    GoalStatus.PAUSED.value,
                ):
                    return
                cancelled = self._cancel_event.is_set()
                pause_requested = self._pause_event.is_set()
                in_flight = self._act_in_flight

            if workspace_generation() != state.workspace_generation:
                self._fail(
                    state,
                    StopReason.workspace_changed,
                    "workspace switched while goal was active",
                )
                return
            if cancelled:
                self._cancel(state, "user requested cancel")
                return
            if pause_requested and not in_flight:
                self._pause(state, "user_pause")
                return
            if goal_permission_pending():
                clear_goal_permission_flags()  # consume the one-shot signal
                self._pause(
                    state,
                    "permission_wait",
                    stop_reason=StopReason.permission_wait.value,
                )
                return
            if state.phase not in (
                GoalPhase.VERIFY.value,
                GoalPhase.EVALUATE.value,
            ):
                decision = check_stop(
                    state,
                    now=time.time(),
                    cancelled=False,
                    permission_pending=False,
                )
                if decision.stop:
                    if decision.terminal_status == GoalStatus.PAUSED.value:
                        self._pause(state, decision.reason or "stop")
                    else:
                        self._fail(
                            state,
                            decision.reason or StopReason.internal_error,
                            decision.detail,
                        )
                    return
            try:
                self._step_once(state)
            except Exception as exc:
                self._fail(
                    state,
                    StopReason.internal_error,
                    f"{type(exc).__name__}: {exc}",
                )
                return

    # --- phase implementations ----------------------------------------------

    def _step_once(self, state: GoalState) -> None:
        phase = state.phase
        if phase == GoalPhase.INITIALIZE.value:
            self._initialize(state)
        elif phase == GoalPhase.SELECT_FEATURE.value:
            self._select_feature(state)
        elif phase == GoalPhase.CLAIM.value:
            self._claim(state)
        elif phase == GoalPhase.ACT.value:
            self._act(state)
        elif phase == GoalPhase.VERIFY.value:
            self._verify(state)
        elif phase == GoalPhase.EVALUATE.value:
            self._evaluate(state)
        elif phase == GoalPhase.CLEAN_CHECK.value:
            self._clean_check(state)
        else:
            raise GoalNotRunningError(f"cannot step from terminal phase {phase}")

    def _initialize(self, state: GoalState) -> None:
        from harness.tasks import attach_feature, claim_task, create_task
        from harness.features import create_feature

        short = state.target.strip()[:40] or "goal"
        task = create_task(subject=f"Goal: {short}", description=state.target)
        feature = create_feature(
            name=short,
            behavior=state.target,
            verification=state.verification,
            workspace=Path(state.workspace),
            task_id=task.id,
            evaluation_required=state.evaluation_required,
        )
        attach_feature(task.id, feature.id)
        claim_task(task.id, owner=f"goal:{state.id}")
        state.task_id = task.id
        state.feature_id = feature.id
        state.workspace = str(Path(state.workspace))
        save_goal(state)
        self._apply(state, GoalPhase.SELECT_FEATURE, "initialize_complete")

    def _select_feature(self, state: GoalState) -> None:
        from harness.features import feature_is_stale, get_feature, reopen_feature
        from harness.tasks import load_task

        try:
            load_task(state.task_id)
        except FileNotFoundError:
            self._fail(
                state,
                StopReason.missing_dependency,
                f"task {state.task_id} is missing",
            )
            return
        try:
            feature = get_feature(state.feature_id, workspace=Path(state.workspace))
        except FileNotFoundError:
            self._fail(
                state,
                StopReason.missing_dependency,
                f"feature {state.feature_id} is missing",
            )
            return

        if feature.state == "passing":
            if feature_is_stale(feature):
                reopen_feature(state.feature_id, workspace=Path(state.workspace))
                self._apply(state, GoalPhase.ACT, "reopen_stale")
            else:
                self._apply(state, GoalPhase.CLEAN_CHECK, "already_passing")
            return
        if feature.state in ("active", "failing", "blocked"):
            if feature.state == "blocked":
                reopen_feature(state.feature_id, workspace=Path(state.workspace))
            self._apply(state, GoalPhase.ACT, "resume_existing_work")
            return
        self._apply(state, GoalPhase.CLAIM, "feature_not_started")

    def _claim(self, state: GoalState) -> None:
        from harness.features import claim_feature

        claim_feature(state.feature_id, workspace=Path(state.workspace))
        self._apply(state, GoalPhase.ACT, "claimed")

    def _act(self, state: GoalState) -> None:
        from harness.features import get_feature
        from harness.goal.prompt import build_goal_act_prompt

        before = self._progress_snapshot(state)
        feature = get_feature(state.feature_id, workspace=Path(state.workspace))
        self._history.append(
            {"role": "user", "content": build_goal_act_prompt(state, feature)}
        )

        with self._lock:
            if self._cancel_event.is_set() or self._pause_event.is_set():
                return
            clear_cancel()
            if self._cancel_event.is_set() or self._pause_event.is_set():
                return
            self._act_in_flight = True
            state.status = GoalStatus.RUNNING.value
            save_goal(state)

        stats = LoopStats()
        try:
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            with agent_lock:
                agent_loop(
                    self._history,
                    self._context,
                    max_rounds=state.max_rounds_per_attempt,
                    binding=self._binding,
                    disabled_tools=ACT_DISABLED_TOOLS,
                    stats=stats,
                )
        finally:
            set_goal_noninteractive(False)
            with self._lock:
                self._act_in_flight = False

        state.total_llm_rounds += stats.llm_rounds
        state.attempts += 1
        if self._progress_snapshot(state) == before:
            state.no_progress_count += 1
        else:
            state.no_progress_count = 0

        with self._lock:
            cancelled = self._cancel_event.is_set()
            paused = self._pause_event.is_set()
        if cancelled:
            self._cancel(state, "user requested cancel")
            return
        if goal_permission_pending():
            clear_goal_permission_flags()  # consume the one-shot signal
            self._pause(
                state,
                "permission_wait",
                stop_reason=StopReason.permission_wait.value,
            )
            return
        if paused:
            self._pause(state, "user_pause")
            return
        self._apply(state, GoalPhase.VERIFY, "agent_loop_finished")

    def _verify(self, state: GoalState) -> None:
        from harness.features import get_feature, reopen_feature

        # Re-verifying an already-passing feature: the feature store does not
        # allow passing -> passing directly, so reopen it first (the runner's
        # verification is the only gate into passing).
        feature = get_feature(state.feature_id, workspace=Path(state.workspace))
        if feature.state == "passing":
            feature = reopen_feature(state.feature_id, workspace=Path(state.workspace))
        feature = verify_feature_command(
            state.feature_id,
            workspace=Path(state.workspace),
        )
        if feature.state == "passing":
            state.consecutive_failures = 0
            state.last_error = None
            if feature.evaluation_required:
                self._apply(state, GoalPhase.EVALUATE, "verification_passed")
            else:
                self._apply(state, GoalPhase.CLEAN_CHECK, "verification_passed")
            return
        error = feature.last_error or f"verification failed (state={feature.state})"
        state.consecutive_failures += 1
        state.last_error = error
        if error and (
            "policy rejected" in error or "permission engine" in error
        ):
            self._fail(state, StopReason.verification_policy_rejected, error)
            return
        self._apply(state, GoalPhase.ACT, "verification_failed", error=error)

    def _evaluate(self, state: GoalState) -> None:
        try:
            feature = run_evaluation(state.feature_id, workspace=state.workspace)
            if feature.evaluation and feature.evaluation.get("error"):
                state.last_error = str(feature.evaluation["error"])[:300]
        except Exception as exc:
            state.last_error = f"evaluation failed: {exc}"
        self._apply(state, GoalPhase.CLEAN_CHECK, "evaluation_done")

    def _clean_check(self, state: GoalState) -> None:
        from harness.tasks import complete_task

        report = run_clean_check(Path(state.workspace), mode="enforce")
        if report.ok:
            try:
                result = complete_task(state.task_id)
            except FileNotFoundError:
                self._fail(
                    state,
                    StopReason.missing_dependency,
                    f"task {state.task_id} is missing",
                )
                return
            if str(result).startswith("Cannot complete"):
                self._fail(state, StopReason.clean_check_failed, result)
                return
            self._apply(state, GoalPhase.DONE, "clean_checks_passed")
            return
        state.last_error = report.summary()
        state.consecutive_failures += 1
        if (
            state.consecutive_failures >= state.max_consecutive_failures
            or state.attempts >= state.max_attempts
        ):
            self._fail(state, StopReason.clean_check_failed, report.summary())
            return
        self._apply(state, GoalPhase.ACT, "clean_check_failed", error=state.last_error)

    # --- helpers -------------------------------------------------------------

    def _progress_snapshot(self, state: GoalState) -> tuple:
        """(feature file mtime, feature last_error, code snapshot) — used for
        the no-progress fuse."""
        from harness.features import get_feature
        from harness.verification.snapshot import capture_code_snapshot

        try:
            feature = get_feature(state.feature_id, workspace=Path(state.workspace))
            mtime = feature.updated_at
            error = feature.last_error
        except FileNotFoundError:
            mtime, error = 0.0, None
        code = capture_code_snapshot(state.workspace)
        return (mtime, error, code)

    def _apply(
        self,
        state: GoalState,
        target: str,
        reason: str,
        *,
        error: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        self.engine.transition(
            state,
            target,
            reason,
            error=error,
            stop_reason=stop_reason,
        )
        save_goal(state)
        _emit(
            "goal_phase",
            id=state.id,
            phase=state.phase,
            status=state.status,
            feature_id=state.feature_id,
            attempt=state.attempts,
        )

    def _pause(self, state: GoalState, reason: str, *, stop_reason: str | None = None) -> None:
        self._apply(state, GoalPhase.PAUSED, reason, stop_reason=stop_reason)

    def _cancel(self, state: GoalState, detail: str = "") -> None:
        if detail:
            state.last_error = detail
        self._apply(
            state,
            GoalPhase.CANCELLED,
            StopReason.cancelled_by_user.value,
            error=state.last_error,
            stop_reason=StopReason.cancelled_by_user.value,
        )

    def _fail(
        self,
        state: GoalState,
        reason: StopReason | str,
        detail: str = "",
    ) -> None:
        reason_value = (
            reason.value if isinstance(reason, StopReason) else str(reason)
        )
        if detail:
            state.last_error = detail
        self._apply(
            state,
            GoalPhase.FAILED,
            reason_value,
            error=state.last_error,
            stop_reason=reason_value,
        )

    def _archive_terminal(self) -> None:
        if self._archived:
            return
        with self._lock:
            state = self._state
            if state is None:
                return
            if state.status in (
                GoalStatus.DONE.value,
                GoalStatus.FAILED.value,
                GoalStatus.CANCELLED.value,
            ):
                try:
                    archive_goal(state)
                    self._archived = True
                    _emit(
                        "goal_stopped",
                        id=state.id,
                        status=state.status,
                        phase=state.phase,
                        stop_reason=state.stop_reason,
                    )
                except OSError:
                    pass

