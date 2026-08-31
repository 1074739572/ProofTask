"""Feature primitive: behavior + verification + state + evidence (L2).

A *feature* is the smallest unit of *user-visible* behavior that the harness
tracks across sessions.  It is deliberately distinct from:

- todo  — in-session steps the agent plans for itself (``harness/todos``);
- task  — cross-session scheduling unit with owner/dependencies (``harness/tasks``).

A feature owns:

- ``behavior``: what the user asked for, in verifiable terms;
- ``verification``: how to prove it (command / check id / oracle);
- ``state``: the state-machine value (see ``harness.features.state``);
- ``evidence``: the proof that moved the feature into ``passing``.

Design rule (from the reliability plan §0.3): a feature may only enter
``passing`` through a verification result — an agent's self-reported
"done" has no programmatic effect on feature state.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


# --- State machine -----------------------------------------------------------

#: Valid single-step transitions.
#: - ``passing`` can be reached ONLY via ``verify_feature(passed=True, evidence=...)``.
#: - ``passing`` is NOT terminal: re-verification can push it back to ``failing``.
#: - ``active`` reopens a passing/failing feature for further work.
TRANSITIONS: dict[str, set[str]] = {
    "not_started": {"active", "blocked"},
    "active": {"passing", "failing", "blocked"},
    "blocked": {"active"},
    "failing": {"active", "passing", "failing"},  # failing->failing: idempotent retry
    "passing": {"failing", "active"},
}

VALID_STATES = frozenset(TRANSITIONS)


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


# --- Data model --------------------------------------------------------------

@dataclass(frozen=True)
class VerificationEvidence:
    """Proof attached to a passing/failing verdict."""

    command: str
    exit_code: int
    stdout_tail: str = ""
    duration_ms: float = 0.0
    verified_by: str = "oracle"  # oracle | runner | harness
    code_snapshot: str = ""  # git HEAD:worktree-fingerprint at verification time
    selectors: tuple[str, ...] = ()
    collected_count: int = 0
    # Structured explanation of non-zero test output.  This is deliberately
    # advisory: the exit code remains the source of truth for the verdict.
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Feature:
    id: str
    name: str
    behavior: str
    verification: str
    state: str = "not_started"
    workspace: str = ""
    task_id: str | None = None
    # Goal Task contract. Feature remains an internal execution projection
    # during migration, so it persists the Task's acceptance and test binding.
    acceptance_cases: list[dict[str, Any]] = field(default_factory=list)
    verification_spec: dict[str, Any] = field(default_factory=dict)
    # L6 v2: dependency edges between goal features (ids of features that must
    # be passing before this one starts).
    depends_on: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    # L5: independent evaluator findings (advisory; never gates state).
    evaluation: dict[str, Any] | None = None
    # L5: explicit opt-in for an independent evaluator pass (simple tasks skip).
    evaluation_required: bool = False

    @classmethod
    def new(
        cls,
        name: str,
        behavior: str,
        verification: str,
        *,
        workspace: str = "",
        task_id: str | None = None,
        evaluation_required: bool = False,
        depends_on: list[str] | None = None,
        acceptance_cases: list[dict[str, Any]] | None = None,
        verification_spec: dict[str, Any] | None = None,
    ) -> "Feature":
        now = time.time()
        return cls(
            id=f"feat_{int(now)}_{uuid.uuid4().hex[:6]}",
            name=name,
            behavior=behavior,
            verification=verification,
            workspace=str(workspace or ""),
            task_id=task_id,
            acceptance_cases=list(acceptance_cases or []),
            verification_spec=dict(verification_spec or {}),
            evaluation_required=evaluation_required,
            depends_on=depends_on or [],
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
