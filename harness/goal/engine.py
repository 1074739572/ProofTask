"""Pure goal state machine (L6 /goal).

``GoalEngine`` only mutates the in-memory :class:`GoalState` — it never
touches the LLM, filesystem, features or tasks. Persisting happens in the
runner; tests drive the engine directly.
"""

from __future__ import annotations

import time

from harness.goal.models import (
    MAX_TRANSITION_LOG,
    GoalPhase,
    GoalStatus,
    GoalState,
)

#: Any non-terminal phase may be paused, cancelled, or hard-stopped (spec
#: §5.3: ACT -> PAUSED/CANCELLED/FAILED; VERIFY -> FAILED; missing dependency
#: during SELECT_TASK/INITIALIZE must be able to FAILED).
_TERMINATING_TARGETS = frozenset(
    {GoalPhase.PAUSED.value, GoalPhase.CANCELLED.value, GoalPhase.FAILED.value}
)


def _with_terminal_escapes(legal: dict[str, set[str]]) -> dict[str, set[str]]:
    legal = {phase: set(targets) for phase, targets in legal.items()}
    for phase in legal:
        if phase in (GoalPhase.DONE.value, GoalPhase.CANCELLED.value, GoalPhase.FAILED.value):
            continue
        legal[phase] |= _TERMINATING_TARGETS
    return legal


#: Legal single-step phase transitions (docs/goal-mode-mvp-spec.md §5.3),
#: including pause/cancel/hard-stop escape hatches on every non-terminal phase.
LEGAL: dict[str, set[str]] = _with_terminal_escapes(
    {
        GoalPhase.INITIALIZE.value: {GoalPhase.SELECT_TASK.value, GoalPhase.PREPARE_TESTS.value},
        GoalPhase.PREPARE_TESTS.value: {GoalPhase.PAUSED.value, GoalPhase.SELECT_TASK.value},
        GoalPhase.SELECT_TASK.value: {GoalPhase.PREPARE_TESTS.value, GoalPhase.CLAIM.value, GoalPhase.ACT.value, GoalPhase.CLEAN_CHECK.value, GoalPhase.FULL_VERIFY.value},
        GoalPhase.CLAIM.value: {GoalPhase.ACT.value},
        GoalPhase.ACT.value: {GoalPhase.ROLLOVER.value, GoalPhase.VERIFY.value, GoalPhase.REPAIR_PLAN.value},
        GoalPhase.ROLLOVER.value: {GoalPhase.VERIFY.value},
        GoalPhase.VERIFY.value: {GoalPhase.ACT.value, GoalPhase.EVALUATE.value, GoalPhase.CLEAN_CHECK.value, GoalPhase.SELECT_TASK.value, GoalPhase.FULL_VERIFY.value, GoalPhase.REPAIR_PLAN.value},
        GoalPhase.EVALUATE.value: {GoalPhase.CLEAN_CHECK.value, GoalPhase.REPAIR_PLAN.value},
        GoalPhase.REPAIR_PLAN.value: {GoalPhase.ACT.value, GoalPhase.PREPARE_TESTS.value, GoalPhase.SELECT_TASK.value},
        GoalPhase.CLEAN_CHECK.value: {GoalPhase.DONE.value, GoalPhase.ACT.value, GoalPhase.IMPACT_REVIEW.value},
        GoalPhase.IMPACT_REVIEW.value: {GoalPhase.SELECT_TASK.value, GoalPhase.PREPARE_TESTS.value},
        GoalPhase.FULL_VERIFY.value: {GoalPhase.CLEAN_CHECK.value, GoalPhase.DONE.value, GoalPhase.REPAIR_PLAN.value},
        GoalPhase.PAUSED.value: {
            GoalPhase.INITIALIZE.value,
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
        },
        GoalPhase.DONE.value: set(),
        GoalPhase.CANCELLED.value: set(),
        GoalPhase.FAILED.value: set(),
    }
)

_STATUS_FOR_PHASE: dict[str, str] = {
    GoalPhase.INITIALIZE.value: GoalStatus.RUNNING.value,
    GoalPhase.SELECT_TASK.value: GoalStatus.RUNNING.value,
    GoalPhase.PREPARE_TESTS.value: GoalStatus.RUNNING.value,
    GoalPhase.CLAIM.value: GoalStatus.RUNNING.value,
    GoalPhase.ACT.value: GoalStatus.RUNNING.value,
    GoalPhase.ROLLOVER.value: GoalStatus.RUNNING.value,
    GoalPhase.VERIFY.value: GoalStatus.RUNNING.value,
    GoalPhase.EVALUATE.value: GoalStatus.RUNNING.value,
    GoalPhase.REPAIR_PLAN.value: GoalStatus.RUNNING.value,
    GoalPhase.IMPACT_REVIEW.value: GoalStatus.RUNNING.value,
    GoalPhase.CLEAN_CHECK.value: GoalStatus.RUNNING.value,
    GoalPhase.FULL_VERIFY.value: GoalStatus.RUNNING.value,
    GoalPhase.DONE.value: GoalStatus.DONE.value,
    GoalPhase.PAUSED.value: GoalStatus.PAUSED.value,
    GoalPhase.CANCELLED.value: GoalStatus.CANCELLED.value,
    GoalPhase.FAILED.value: GoalStatus.FAILED.value,
}


class GoalTransitionError(ValueError):
    """Raised on illegal state-machine transitions (G001 covers this)."""


class GoalEngine:
    """Pure transition engine. Thread-hostile by design: the runner serializes."""

    def initialize(self, state: GoalState) -> GoalState:
        """INITIALIZE -> SELECT_TASK after durable Task creation."""
        return self.transition(state, GoalPhase.SELECT_TASK, "initialize_complete")

    def transition(
        self,
        state: GoalState,
        target: GoalPhase | str,
        reason: str,
        *,
        error: str | None = None,
        stop_reason: str | None = None,
    ) -> GoalState:
        """Move the state machine one step. Raises GoalTransitionError if illegal.

        ``reason`` is the human/log transition reason; ``stop_reason`` records
        a terminal reason for PAUSED/CANCELLED/FAILED.
        """
        target = target.value if isinstance(target, GoalPhase) else str(target)
        current = state.phase
        if target not in LEGAL.get(current, set()):
            raise GoalTransitionError(
                f"illegal goal transition {current} -> {target} "
                f"(allowed: {sorted(LEGAL.get(current, set()))})"
            )

        state.last_phase = current
        state.phase = target
        state.status = _STATUS_FOR_PHASE[target]
        state.updated_at = time.time()
        if error is not None:
            state.last_error = error

        if target == GoalPhase.PAUSED.value:
            # Keep the source phase; a restart must never guess that it is safe
            # to jump straight into implementation.
            state.resume_phase = current
            state.paused_at = state.paused_at or time.time()
            if stop_reason is not None:
                state.stop_reason = stop_reason
        elif target in (GoalPhase.CANCELLED.value, GoalPhase.FAILED.value):
            state.completed_at = state.completed_at or time.time()
            if stop_reason is not None:
                state.stop_reason = stop_reason
            elif state.stop_reason is None:
                state.stop_reason = reason
        elif target == GoalPhase.DONE.value:
            state.completed_at = state.completed_at or time.time()

        state.transition_log.append(
            {
                "from": current,
                "to": target,
                "at": time.time(),
                "reason": reason,
                "attempt": state.attempts,
            }
        )
        if len(state.transition_log) > MAX_TRANSITION_LOG:
            del state.transition_log[:-MAX_TRANSITION_LOG]
        return state
