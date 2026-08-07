"""Goal state model (L6 /goal autonomous execution).

The goal state is a single persisted dataclass that survives across sessions.
Every transition goes through ``harness.goal.engine.GoalEngine`` which keeps
the state machine legal and appends to ``transition_log``.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

GOAL_SCHEMA_VERSION = 1

DEFAULT_MAX_ROUNDS_PER_ATTEMPT = 20
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_MAX_DURATION_SECONDS = 1800  # 30 minutes
MAX_TRANSITION_LOG = 100


class GoalPhase(str, Enum):
    INITIALIZE = "initialize"
    SELECT_FEATURE = "select_feature"
    CLAIM = "claim"
    ACT = "act"
    VERIFY = "verify"
    EVALUATE = "evaluate"  # conditional; MVP runs at most once
    CLEAN_CHECK = "clean_check"
    FULL_VERIFY = "full_verify"  # L6 v2: whole-goal verification gate
    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GoalStatus(str, Enum):
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(str, Enum):
    max_duration = "max_duration"
    max_attempts = "max_attempts"
    max_consecutive_failures = "max_consecutive_failures"
    max_rounds = "max_rounds"
    permission_wait = "permission_wait"
    no_progress = "no_progress"
    cancelled_by_user = "cancelled_by_user"
    process_restarted = "process_restarted"
    missing_dependency = "missing_dependency"
    verification_policy_rejected = "verification_policy_rejected"
    clean_check_failed = "clean_check_failed"
    workspace_changed = "workspace_changed"
    # L6 v2: a decomposed feature failed or the full-verification gate failed.
    feature_failed = "feature_failed"
    full_verification_failed = "full_verification_failed"
    internal_error = "internal_error"


_VALID_PHASES = frozenset(phase.value for phase in GoalPhase)
_VALID_STATUSES = frozenset(status.value for status in GoalStatus)


@dataclass
class GoalState:
    """Persisted goal state (see docs/goal-mode-mvp-spec.md §6.2)."""

    schema_version: int = GOAL_SCHEMA_VERSION
    id: str = ""
    target: str = ""
    verification: str = ""
    phase: str = GoalPhase.INITIALIZE.value
    status: str = GoalStatus.RUNNING.value
    workspace: str = ""
    task_id: str | None = None
    feature_id: str | None = None
    # L6 v2 decomposition: all features of this goal, in plan order.
    # Empty = legacy single-feature goal (feature_id is the one feature).
    feature_ids: list[str] = field(default_factory=list)

    max_rounds_per_attempt: int = DEFAULT_MAX_ROUNDS_PER_ATTEMPT
    max_total_rounds: int = DEFAULT_MAX_ROUNDS_PER_ATTEMPT * DEFAULT_MAX_ATTEMPTS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS

    attempts: int = 0
    consecutive_failures: int = 0
    no_progress_count: int = 0
    total_llm_rounds: int = 0
    workspace_generation: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    paused_at: float | None = None

    last_phase: str | None = None
    last_error: str | None = None
    stop_reason: str | None = None
    evaluation_required: bool = False
    transition_log: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        target: str,
        verification: str,
        workspace: str,
        workspace_generation: int = 0,
        evaluation_required: bool = False,
        **limits: int,
    ) -> "GoalState":
        state = cls(
            id=f"goal_{int(time.time())}",
            target=target,
            verification=verification,
            workspace=workspace,
            workspace_generation=workspace_generation,
            evaluation_required=evaluation_required,
        )
        for key, value in limits.items():
            if hasattr(state, key):
                setattr(state, key, value)
        if state.max_total_rounds <= 0:
            state.max_total_rounds = state.max_rounds_per_attempt * state.max_attempts
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalState":
        data = dict(data)
        for key in list(data):
            if key not in cls.__dataclass_fields__:
                data.pop(key, None)
        if data.get("phase") not in _VALID_PHASES:
            raise ValueError(f"unknown goal phase {data.get('phase')!r}")
        if data.get("status") not in _VALID_STATUSES:
            raise ValueError(f"unknown goal status {data.get('status')!r}")
        if data.get("schema_version") is None:
            raise ValueError("missing schema_version")
        return cls(**data)
