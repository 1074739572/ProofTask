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
from harness.agents.runner import AgentTaskStats
from harness.clean import run_clean_check
from harness.evaluation import run_evaluation
from harness.goal.engine import GoalEngine
from harness.goal.models import (
    GoalPhase,
    GoalStatus,
    GoalState,
    StopReason,
)
from harness.goal.policy import check_stop, validate_limits
from harness.goal.planner import PLANNER_MAX_ROUNDS, plan_features
from harness.goal.store import (
    GoalStoreError,
    archive_goal,
    load_goal,
    save_goal,
)
from harness.loop import LoopStats, agent_lock, agent_loop
from harness.project.resume import checkpoint_history
from harness.settings import get_workdir, workspace_generation
from harness.verification import run_verification, verify_feature_command

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
    ]
    feature_ids = list(state.feature_ids) if state.feature_ids else ([state.feature_id] if state.feature_id else [])
    if feature_ids:
        from harness.features import get_feature

        done = 0
        marks: list[str] = []
        for fid in feature_ids:
            try:
                feature = get_feature(fid, workspace=Path(state.workspace))
                status = feature.state
                name = feature.name
                verify = feature.verification
            except Exception:
                status, name, verify = "missing", "?", ""
            if status == "passing":
                done += 1
                marks.append(f"    \u2713 {fid} [{status}] {name[:40]}")
            elif fid == state.feature_id:
                marks.append(f"    \u2192 {fid} [{status}] {name[:40]}")
            else:
                marks.append(f"    \u00b7 {fid} [{status}] {name[:40]}")
            if verify:
                marks.append(f"        verify: {verify[:80]}")
        lines.append(f"  Features: {done}/{len(feature_ids)} done")
        lines.extend(marks)
        if state.phase == GoalPhase.PAUSED.value and any(
            t["reason"] == "plan_ready" for t in state.transition_log
        ):
            lines.append("  Plan ready — review it, then /goal resume to approve execution")
    lines.append(f"  Attempt: {state.attempts}/{state.max_attempts}")
    lines.append(f"  Elapsed: {int(elapsed)}s / {state.max_duration_seconds}s")
    lines.append(f"  Consecutive failures: {state.consecutive_failures}/{state.max_consecutive_failures}")
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
    # Only the live in-process runner counts as "running". A running/pausing
    # goal found on disk is normalized to paused by load_goal() (process
    # restart recovery) and must be explicitly resumed — checking it here
    # would be dead code.
    with _runner_lock:
        runner = _runner
        return runner is not None and runner.is_alive() and runner.is_running()


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
        existing = load_goal()
        if existing is not None and existing.status not in (
            GoalStatus.DONE.value,
            GoalStatus.FAILED.value,
            GoalStatus.CANCELLED.value,
        ):
            raise GoalBusyError(
                f"Goal {existing.id} is {existing.status}. Resume, cancel, or finish it before starting another goal."
            )
        if existing is not None:
            # The current slot will be replaced below; retain terminal runs in
            # history even if an earlier worker could not archive them.
            archive_goal(existing)
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
        problems = validate_limits(state)
        if problems:
            raise ValueError("invalid goal limits: " + "; ".join(problems))
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
        # State hygiene on resume: drop the old stop reason / pause timestamp,
        # and exclude paused time from the duration budget (max_duration must
        # count only active execution, not time spent paused).
        state.stop_reason = None
        if state.paused_at is not None:
            state.started_at += time.time() - state.paused_at
        state.paused_at = None
        # ``workspace_generation`` is process-local. The path check above is
        # the durable identity test; bind a resumed run to this process.
        state.workspace_generation = workspace_generation()
        engine = GoalEngine()
        resume_phase = (
            GoalPhase.SELECT_FEATURE
            if state.initialization_complete
            else GoalPhase.INITIALIZE
        )
        state = engine.transition(state, resume_phase, "resumed")
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
        self._phase_in_flight = False
        self._archived = False
        self.engine = GoalEngine()

    # --- public control (called from other threads) -------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._state.status in (
                GoalStatus.RUNNING.value,
                GoalStatus.PAUSING.value,
                GoalStatus.CANCELLING.value,
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
            in_flight = self._phase_in_flight
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
                GoalStatus.CANCELLING.value,
            ):
                raise GoalNotRunningError("Goal is not running.")
            state.status = GoalStatus.CANCELLING.value
            state.cancellation_requested_at = state.cancellation_requested_at or time.time()
            save_goal(state)
            self._cancel_event.set()
            in_flight = self._phase_in_flight
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
                self._phase_in_flight = False
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
                GoalPhase.FULL_VERIFY.value,
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
        elif phase == GoalPhase.FULL_VERIFY.value:
            self._full_verify(state)
        elif phase == GoalPhase.CLEAN_CHECK.value:
            self._clean_check(state)
        else:
            raise GoalNotRunningError(f"cannot step from terminal phase {phase}")

    def _initialize(self, state: GoalState) -> None:
        from harness.features import create_feature, get_feature, list_features
        from harness.goal.planner import FeaturePlan
        from harness.tasks import attach_feature, claim_task, create_task, list_tasks, load_task

        workspace = Path(state.workspace)
        if not state.feature_plan:
            # Persist the read-only plan before creating any task/feature
            # projection. A restart can then replay the exact same graph.
            stats = AgentTaskStats()
            try:
                clear_goal_permission_flags()
                set_goal_noninteractive(True)
                with agent_lock:
                    with self._lock:
                        if self._cancel_event.is_set() or self._pause_event.is_set():
                            return
                        clear_cancel()
                        self._phase_in_flight = True
                    plans = plan_features(
                        state.target,
                        state.verification,
                        workspace,
                        cancel_check=self._phase_interrupted,
                        deadline=self._deadline(state),
                        stats=stats,
                    )
            finally:
                set_goal_noninteractive(False)
                with self._lock:
                    self._phase_in_flight = False

            state.total_llm_rounds += stats.llm_rounds
            if self._handle_phase_interruption(state, stats):
                return
            state.feature_plan = [plan.to_dict() for plan in plans]
            # ``max_total_rounds`` is an aggregate budget. Feature-local
            # retries, planner work and optional evaluation all consume it.
            per_feature = state.max_rounds_per_attempt * state.max_attempts
            if state.evaluation_required:
                per_feature += PLANNER_MAX_ROUNDS
            state.max_total_rounds = max(
                state.max_total_rounds,
                PLANNER_MAX_ROUNDS + len(plans) * per_feature,
            )
            save_goal(state)
        plans = [FeaturePlan.from_dict(item) for item in state.feature_plan]

        short = state.target.strip()[:40] or "goal"
        task = None
        if state.task_id:
            try:
                task = load_task(state.task_id)
            except FileNotFoundError:
                self._fail(
                    state,
                    StopReason.missing_dependency,
                    f"task {state.task_id} is missing",
                )
                return
        else:
            # Recover the narrow crash window between create_task() and the
            # first goal-state save by using the durable goal_id projection.
            task = next((item for item in list_tasks() if item.goal_id == state.id), None)
            if task is None:
                task = create_task(
                    subject=f"Goal: {short}",
                    description=state.target,
                    goal_id=state.id,
                )
            state.task_id = task.id
            save_goal(state)

        if task.status == "pending":
            claim_task(task.id, owner=f"goal:{state.id}")
            task = load_task(task.id)
        state.workspace = str(workspace)

        # Recover features created just before their ids reached goal.json.
        candidates = list_features(workspace=workspace)
        used_ids = set(state.feature_ids)
        name_to_id: dict[str, str] = {}
        for index, plan in enumerate(plans):
            feature_id = state.feature_ids[index] if index < len(state.feature_ids) else None
            feature = None
            if feature_id:
                try:
                    feature = get_feature(feature_id, workspace=workspace)
                except FileNotFoundError:
                    feature_id = None
            if feature is None:
                matches = [
                    item
                    for item in candidates
                    if item.id not in used_ids
                    and item.task_id == task.id
                    and item.name == plan.name
                    and item.behavior == plan.behavior
                ]
                if len(matches) == 1:
                    feature = matches[0]
                    feature_id = feature.id
                else:
                    deps = [name_to_id[dep] for dep in plan.depends_on]
                    feature = create_feature(
                        name=plan.name,
                        behavior=plan.behavior,
                        verification=plan.verification or state.verification,
                        workspace=workspace,
                        task_id=task.id,
                        evaluation_required=state.evaluation_required,
                        depends_on=deps,
                    )
                    feature_id = feature.id
                    candidates.append(feature)

            if index < len(state.feature_ids):
                state.feature_ids[index] = feature_id
            else:
                state.feature_ids.append(feature_id)
            used_ids.add(feature_id)
            name_to_id[plan.name] = feature_id
            state.feature_id = state.feature_ids[0]
            save_goal(state)
            attach_feature(task.id, feature_id)

        state.initialization_complete = True
        state.feature_id = state.feature_ids[0] if state.feature_ids else None
        save_goal(state)
        if len(state.feature_ids) > 1:
            # L6 v2 confirmation gate: a decomposed plan pauses for approval —
            # /goal status shows the plan, /goal resume approves execution.
            self._apply(state, GoalPhase.PAUSED, "plan_ready")
            return
        self._apply(state, GoalPhase.SELECT_FEATURE, "initialize_complete")

    def _feature_ids(self, state: GoalState) -> list[str]:
        if state.feature_ids:
            return list(state.feature_ids)
        return [state.feature_id] if state.feature_id else []

    def _unmet_dependency(self, state: GoalState, feature: Any) -> str | None:
        """First depends_on id that is not passing-and-fresh, or None."""
        from harness.features import feature_is_stale, get_feature

        for dep_id in feature.depends_on:
            try:
                dep = get_feature(dep_id, workspace=Path(state.workspace))
            except FileNotFoundError:
                return dep_id
            if dep.state != "passing" or feature_is_stale(dep):
                return dep_id
        return None

    def _select_feature(self, state: GoalState) -> None:
        from harness.features import feature_is_stale, get_feature, reopen_feature
        from harness.tasks import attach_feature, claim_task, load_task

        try:
            task = load_task(state.task_id)
        except FileNotFoundError:
            self._fail(
                state,
                StopReason.missing_dependency,
                f"task {state.task_id} is missing",
            )
            return

        # Resumed/legacy goal records can predate the task-feature link created
        # by _initialize(). Restore that invariant before progressing so a
        # freshly re-verified feature can complete its owning task.
        if task.status == "pending":
            claim_task(task.id, owner=f"goal:{state.id}")
            task = load_task(task.id)
        for fid in self._feature_ids(state):
            if fid not in task.feature_ids:
                try:
                    attach_feature(task.id, fid)
                except FileNotFoundError:
                    self._fail(
                        state,
                        StopReason.missing_dependency,
                        f"task {task.id} is not on the active board",
                    )
                    return

        for fid in self._feature_ids(state):
            try:
                feature = get_feature(fid, workspace=Path(state.workspace))
            except FileNotFoundError:
                self._fail(
                    state,
                    StopReason.missing_dependency,
                    f"feature {fid} is missing",
                )
                return
            if feature.state == "passing":
                if feature_is_stale(feature):
                    state.feature_id = fid
                    reopen_feature(fid, workspace=Path(state.workspace))
                    self._apply(state, GoalPhase.ACT, "reopen_stale")
                    return
                continue  # done — next feature
            unmet = self._unmet_dependency(state, feature)
            if unmet is not None:
                self._fail(
                    state,
                    StopReason.missing_dependency,
                    f"feature {fid} depends on incomplete feature {unmet}",
                )
                return
            if fid != state.feature_id:
                # L6 v2: per-feature budgets — a new feature starts with a
                # fresh attempt/no-progress allowance.
                state.attempts = 0
                state.no_progress_count = 0
            state.feature_id = fid
            if feature.state in ("active", "failing", "blocked"):
                if feature.state == "blocked":
                    reopen_feature(fid, workspace=Path(state.workspace))
                self._apply(state, GoalPhase.ACT, "resume_existing_work")
            else:
                self._apply(state, GoalPhase.CLAIM, "feature_not_started")
            return

        # Every feature is passing and fresh.
        if len(self._feature_ids(state)) > 1:
            self._apply(state, GoalPhase.FULL_VERIFY, "all_features_passing")
        else:
            self._apply(state, GoalPhase.CLEAN_CHECK, "already_passing")

    def _full_verify(self, state: GoalState) -> None:
        """Whole-goal gate after a decomposed goal: run the user's --verify
        command across everything (read-only, policy-gated)."""
        result = run_verification(state.verification, workspace=Path(state.workspace))
        if result.passed:
            self._apply(state, GoalPhase.CLEAN_CHECK, "full_verification_passed")
            return
        error = result.error or f"full verification failed with exit code {result.exit_code}"
        self._fail(state, StopReason.full_verification_failed, error)

    def _claim(self, state: GoalState) -> None:
        from harness.features import claim_feature

        claim_feature(state.feature_id, workspace=Path(state.workspace))
        self._apply(state, GoalPhase.ACT, "claimed")

    def _act(self, state: GoalState) -> None:
        from harness.features import get_feature
        from harness.goal.prompt import build_goal_act_prompt

        before = self._progress_snapshot(state)
        feature = get_feature(state.feature_id, workspace=Path(state.workspace))

        stats = LoopStats()
        try:
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            with agent_lock:
                # The prompt append lives inside the lock region: it must never
                # race with a normal turn (history is shared) and must not leave
                # a stray message when the goal was cancelled/paused meanwhile.
                with self._lock:
                    if self._cancel_event.is_set() or self._pause_event.is_set():
                        return
                    clear_cancel()
                    if self._cancel_event.is_set() or self._pause_event.is_set():
                        return
                    self._act_in_flight = True
                    self._phase_in_flight = True
                    state.status = GoalStatus.RUNNING.value
                    save_goal(state)
                    self._history.append(
                        {"role": "user", "content": build_goal_act_prompt(state, feature)}
                    )
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
                self._phase_in_flight = False

        state.total_llm_rounds += stats.llm_rounds
        if self._binding is not None:
            try:
                checkpoint_history(self._history, binding=self._binding)
            except Exception:
                # Checkpoints improve recovery but must not mask the actual
                # goal result when a session backend is temporarily unavailable.
                pass
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

    def _has_pending_feature(self, state: GoalState) -> bool:
        """True when any goal feature is not passing-and-fresh."""
        from harness.features import feature_is_stale, get_feature

        for fid in self._feature_ids(state):
            try:
                feature = get_feature(fid, workspace=Path(state.workspace))
            except FileNotFoundError:
                return True
            if feature.state != "passing" or feature_is_stale(feature):
                return True
        return False

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
            # L6 v2 per-feature budgets: a decomposed goal resets the spent
            # attempt / no-progress allowance when a feature finishes so the
            # next feature starts fresh (single-feature goals keep the MVP
            # cumulative semantics).
            if len(self._feature_ids(state)) > 1:
                state.attempts = 0
                state.no_progress_count = 0
            state.last_error = None
            if feature.evaluation_required:
                self._apply(state, GoalPhase.EVALUATE, "verification_passed")
            elif self._has_pending_feature(state):
                # L6 v2: move on to the next feature in the plan.
                self._apply(state, GoalPhase.SELECT_FEATURE, "verification_passed")
            elif len(self._feature_ids(state)) > 1:
                self._apply(state, GoalPhase.FULL_VERIFY, "all_features_passing")
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
        stats = AgentTaskStats()
        try:
            clear_goal_permission_flags()
            set_goal_noninteractive(True)
            with agent_lock:
                with self._lock:
                    if self._cancel_event.is_set() or self._pause_event.is_set():
                        return
                    clear_cancel()
                    self._phase_in_flight = True
                feature = run_evaluation(
                    state.feature_id,
                    workspace=state.workspace,
                    cancel_check=self._phase_interrupted,
                    deadline=self._deadline(state),
                    stats=stats,
                )
            if feature.evaluation and feature.evaluation.get("error"):
                state.last_error = str(feature.evaluation["error"])[:300]
        except Exception as exc:
            state.last_error = f"evaluation failed: {exc}"
        finally:
            set_goal_noninteractive(False)
            with self._lock:
                self._phase_in_flight = False
        state.total_llm_rounds += stats.llm_rounds
        if self._handle_phase_interruption(state, stats):
            return
        if self._has_pending_feature(state):
            self._apply(state, GoalPhase.SELECT_FEATURE, "evaluation_done")
        else:
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
            # complete_task returns human-readable strings; success means the
            # task reached the completed status.
            if not (
                result.startswith("Completed") or "already completed" in result
            ):
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
        if len(self._feature_ids(state)) > 1:
            # Reopen a concrete feature and return to ACT.  Merely selecting
            # all-passing features would otherwise cycle SELECT -> FULL_VERIFY
            # -> CLEAN_CHECK without giving the agent the failure evidence.
            from harness.features import reopen_feature

            target_id = state.feature_id or self._feature_ids(state)[-1]
            reopen_feature(target_id, workspace=Path(state.workspace))
            state.feature_id = target_id
            self._apply(state, GoalPhase.ACT, "clean_check_failed", error=state.last_error)
            return
        self._apply(state, GoalPhase.ACT, "clean_check_failed", error=state.last_error)

    # --- helpers -------------------------------------------------------------

    def _phase_interrupted(self) -> bool:
        """Cancellation callback for planner/evaluator subagents."""
        return self._cancel_event.is_set() or self._pause_event.is_set()

    def _deadline(self, state: GoalState) -> float:
        remaining = max(0.0, state.max_duration_seconds - (time.time() - state.started_at))
        return time.monotonic() + remaining

    def _handle_phase_interruption(self, state: GoalState, stats: AgentTaskStats) -> bool:
        """Apply control signals before a planner/evaluator result is used."""
        if self._cancel_event.is_set() or stats.stop_reason == "cancelled" and self._cancel_event.is_set():
            self._cancel(state, "user requested cancel")
            return True
        if self._pause_event.is_set():
            self._pause(state, "user_pause")
            return True
        if stats.stop_reason == "deadline" or time.time() - state.started_at >= state.max_duration_seconds:
            self._fail(
                state,
                StopReason.max_duration,
                f"exceeded max_duration_seconds={state.max_duration_seconds}",
            )
            return True
        if goal_permission_pending():
            clear_goal_permission_flags()
            self._pause(
                state,
                "permission_wait",
                stop_reason=StopReason.permission_wait.value,
            )
            return True
        return False

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
        # L6 v2: in a decomposed goal, per-feature retry fuses are attributed
        # to the feature (stop_reason=feature_failed) so it is clear WHICH
        # feature exhausted its budget.
        if (
            len(self._feature_ids(state)) > 1
            and state.feature_id
            and reason_value
            in {
                StopReason.max_attempts.value,
                StopReason.max_consecutive_failures.value,
                StopReason.no_progress.value,
                StopReason.max_rounds.value,
            }
        ):
            try:
                from harness.features import get_feature

                feature = get_feature(state.feature_id, workspace=Path(state.workspace))
                detail = (
                    f"feature {state.feature_id} ({feature.name}) failed: "
                    f"{detail or reason_value}"
                )
            except Exception:
                detail = f"feature {state.feature_id} failed: {detail or reason_value}"
            reason_value = StopReason.feature_failed.value
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

