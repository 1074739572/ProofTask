"""Goal worker-limit validation and progress policy (L6).

``check_stop`` is a pure function — the runner applies it between phases with
the current clock and control flags. Stop order
(docs/goal-mode-mvp-spec.md §7.3):

    user cancel
    -> permission pending
    -> otherwise continue

Long-running Goal work is never stopped because it accumulated time, model
rounds, repair cycles, or failed attempts.  Those facts route to a fresh
worker or the repair planner in the runner.
"""

from __future__ import annotations

from harness.goal.models import GoalState

# A worker is disposable.  Two consecutive workers that made no observable
# progress should ask the repair planner to choose a different strategy.
NO_PROGRESS_REPLAN_LIMIT = 2
# This is a per-Task circuit breaker, not a Goal lifetime budget. A repeating
# repair loop has stopped producing new evidence and needs a human decision.
MAX_REPAIR_ATTEMPTS_PER_TASK = 4


def validate_limits(state: GoalState) -> list[str]:
    """Return a list of invalid limit problems (empty when valid)."""
    problems: list[str] = []
    if state.worker_round_limit <= 0:
        problems.append("worker_round_limit must be positive")
    if state.operation_timeout_seconds <= 0:
        problems.append("operation_timeout_seconds must be positive")
    return problems
