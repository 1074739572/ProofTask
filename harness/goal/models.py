"""Goal state model (L6 /goal autonomous execution).

The goal state is a single persisted dataclass that survives across sessions.
Every transition goes through ``harness.goal.engine.GoalEngine`` which keeps
the state machine legal and appends to ``transition_log``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

GOAL_SCHEMA_VERSION = 3

DEFAULT_WORKER_ROUND_LIMIT = 20
DEFAULT_OPERATION_TIMEOUT_SECONDS = 1800  # one agent operation, not the Goal
MAX_TRANSITION_LOG = 100


class GoalPhase(str, Enum):
    INITIALIZE = "initialize"
    SELECT_TASK = "select_task"
    PREPARE_TESTS = "prepare_tests"
    CLAIM = "claim"
    ACT = "act"
    ROLLOVER = "rollover"
    VERIFY = "verify"
    EVALUATE = "evaluate"  # conditional; MVP runs at most once
    REPAIR_PLAN = "repair_plan"
    IMPACT_REVIEW = "impact_review"
    CLEAN_CHECK = "clean_check"
    FULL_VERIFY = "full_verify"  # L6 v2: whole-goal verification gate
    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class GoalStatus(str, Enum):
    RUNNING = "running"
    PAUSING = "pausing"
    CANCELLING = "cancelling"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(str, Enum):
    permission_wait = "permission_wait"
    cancelled_by_user = "cancelled_by_user"
    process_restarted = "process_restarted"
    missing_dependency = "missing_dependency"
    verification_policy_rejected = "verification_policy_rejected"
    clean_check_failed = "clean_check_failed"
    workspace_changed = "workspace_changed"
    task_failed = "task_failed"
    test_generation_required = "test_generation_required"
    user_approval_required = "user_approval_required"
    full_verification_failed = "full_verification_failed"
    full_verification_interrupted = "full_verification_interrupted"
    evaluation_unavailable = "evaluation_unavailable"
    impact_review_format_error = "impact_review_format_error"
    repair_plan_format_error = "repair_plan_format_error"
    provider_unavailable = "provider_unavailable"
    autonomy_blocked = "autonomy_blocked"
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
    # The plan is persisted before task creation.  Each item maps to exactly
    # one durable Task; there is no Feature projection in Goal mode.
    task_ids: list[str] = field(default_factory=list)
    # Stable plan-name -> Task id mapping makes INITIALIZE restartable when a
    # process stops between individual Task creations.
    task_name_ids: dict[str, str] = field(default_factory=dict)
    current_task_id: str | None = None
    task_plan: list[dict[str, Any]] = field(default_factory=list)
    # The confirmed product boundary is immutable after `/goal run`; Task plans
    # may evolve inside it, but execution must not fall back to chat history.
    goal_contract: dict[str, Any] = field(default_factory=dict)
    initialization_complete: bool = False
    # The exact durable phase to resume after a pause/restart.
    resume_phase: str | None = None

    # Long Goals are unbounded by default.  Only one disposable worker and one
    # external operation are bounded; worker rollover preserves durable state.
    worker_round_limit: int = DEFAULT_WORKER_ROUND_LIMIT
    operation_timeout_seconds: int = DEFAULT_OPERATION_TIMEOUT_SECONDS

    attempts: int = 0
    consecutive_failures: int = 0
    no_progress_count: int = 0
    total_llm_rounds: int = 0
    repair_attempts: int = 0
    worker_generation: int = 0
    worker_rollovers: int = 0
    workspace_generation: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    paused_at: float | None = None
    cancellation_requested_at: float | None = None

    last_phase: str | None = None
    last_error: str | None = None
    stop_reason: str | None = None
    # Durable machine evidence for the Goal-level regression command. Task
    # evidence proves each unit; this records the final whole-goal gate.
    final_verification: dict[str, Any] | None = None
    # Draft-backed Goals stop after test generation and a failing baseline.
    # Only an explicit `/goal run` permits production implementation.
    execution_approved: bool = True
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
            # Second-granularity ids collide when a goal is restarted within
            # the same second (overwriting the history archive) — add a short
            # random suffix for uniqueness.
            id=f"goal_{int(time.time())}_{uuid.uuid4().hex[:4]}",
            target=target,
            verification=verification,
            workspace=workspace,
            workspace_generation=workspace_generation,
            evaluation_required=evaluation_required,
        )
        for key, value in limits.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalState":
        data = dict(data)
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unsupported goal fields: {sorted(unknown)}")
        if data.get("phase") not in _VALID_PHASES:
            raise ValueError(f"unknown goal phase {data.get('phase')!r}")
        if data.get("status") not in _VALID_STATUSES:
            raise ValueError(f"unknown goal status {data.get('status')!r}")
        if data.get("schema_version") is None:
            raise ValueError("missing schema_version")
        return cls(**data)
