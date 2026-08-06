"""Goal limits validation and hard-stop decisions (L6).

``check_stop`` is a pure function — the runner applies it between phases with
the current clock and control flags. Stop order
(docs/goal-mode-mvp-spec.md §7.3):

    user cancel
    -> permission pending
    -> max duration
    -> max attempts
    -> max consecutive failures
    -> max rounds
    -> no progress
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.goal.models import (
    GoalPhase,
    GoalStatus,
    GoalState,
    StopReason,
)

#: Phases where "we are about to start another attempt" checks apply. VERIFY,
#: EVALUATE and CLEAN_CHECK are excluded so the last attempt is always verified
#: and the terminal clean check is always allowed to run before the
#: attempt/consecutive-failure caps can stop the goal (the CLEAN_CHECK phase
#: enforces its own cap inline via ``clean_check_failed``).
_PRE_ACT_PHASES = frozenset(
    {
        GoalPhase.INITIALIZE.value,
        GoalPhase.SELECT_FEATURE.value,
        GoalPhase.CLAIM.value,
        GoalPhase.ACT.value,
    }
)

#: Consecutive no-progress ACTs that trigger the no-progress fuse (MVP).
NO_PROGRESS_LIMIT = 2


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    terminal_status: str | None = None
    reason: str | None = None
    detail: str = ""


def validate_limits(state: GoalState) -> list[str]:
    """Return a list of invalid limit problems (empty when valid)."""
    problems: list[str] = []
    if state.max_rounds_per_attempt <= 0:
        problems.append("max_rounds_per_attempt must be positive")
    if state.max_total_rounds <= 0:
        problems.append("max_total_rounds must be positive")
    if state.max_attempts <= 0:
        problems.append("max_attempts must be positive")
    if state.max_consecutive_failures <= 0:
        problems.append("max_consecutive_failures must be positive")
    if state.max_duration_seconds <= 0:
        problems.append("max_duration_seconds must be positive")
    return problems


def check_stop(
    state: GoalState,
    *,
    now: float,
    cancelled: bool = False,
    permission_pending: bool = False,
) -> StopDecision:
    """Hard-stop decision in priority order (see module docstring)."""
    if cancelled:
        return StopDecision(
            True,
            GoalStatus.CANCELLED.value,
            StopReason.cancelled_by_user.value,
            "user requested cancel",
        )
    if permission_pending:
        return StopDecision(
            True,
            GoalStatus.PAUSED.value,
            StopReason.permission_wait.value,
            "tool permission requires human approval",
        )
    if now - state.started_at >= state.max_duration_seconds:
        return StopDecision(
            True,
            GoalStatus.FAILED.value,
            StopReason.max_duration.value,
            f"exceeded max_duration_seconds={state.max_duration_seconds}",
        )
    if state.phase in _PRE_ACT_PHASES:
        if state.attempts >= state.max_attempts:
            return StopDecision(
                True,
                GoalStatus.FAILED.value,
                StopReason.max_attempts.value,
                f"reached max_attempts={state.max_attempts}",
            )
        if state.consecutive_failures >= state.max_consecutive_failures:
            return StopDecision(
                True,
                GoalStatus.FAILED.value,
                StopReason.max_consecutive_failures.value,
                f"reached max_consecutive_failures={state.max_consecutive_failures}",
            )
    if state.total_llm_rounds >= state.max_total_rounds:
        return StopDecision(
            True,
            GoalStatus.FAILED.value,
            StopReason.max_rounds.value,
            f"reached max_total_rounds={state.max_total_rounds}",
        )
    if state.no_progress_count >= NO_PROGRESS_LIMIT:
        return StopDecision(
            True,
            GoalStatus.FAILED.value,
            StopReason.no_progress.value,
            f"{state.no_progress_count} consecutive attempts without progress",
        )
    return StopDecision(False)
