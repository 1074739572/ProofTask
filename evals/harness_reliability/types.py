"""Data model for the harness reliability eval suite.

Purpose: answer "does the same model complete the same task more reliably
under a different harness configuration?" — not "is the harness code broken"
(that is evals/cases/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VariantId = Literal["baseline", "instructions", "structured"]

# Statuses of one agent run.
RunStatus = Literal["ok", "error", "timeout", "interrupted"]


@dataclass(frozen=True)
class ReliabilityTask:
    """Static description of one eval task. No per-run state."""

    id: str
    name: str
    category: str
    prompt: str
    fixture: str  # directory name under fixtures/
    max_rounds: int = 25
    requires_multi_session: bool = False
    prompt_session2: str = ""  # required when requires_multi_session=True
    prompt_session3: str = ""  # optional third session (continuity-loss eval)
    allowed_paths: tuple[str, ...] = ()  # files the agent may modify
    oracle_timeout_s: int = 120
    check_script: str = ""  # name under checks/; independent behavior oracle


@dataclass(frozen=True)
class HarnessVariant:
    """One harness configuration to compare. Only one knob changed at a time."""

    id: VariantId
    project_instructions: bool = False  # HARNESS.md present + injected
    progress_state: bool = False  # progress.md + feature_list.json present
    verification_prompt: bool = False  # prompt demands running verification
    wip_constraint: bool = False  # prompt demands one feature at a time


@dataclass
class OracleCheck:
    """One independently executed acceptance check."""

    id: str
    command: str
    passed: bool
    exit_code: int | None = None
    duration_ms: float = 0.0
    detail: str = ""


@dataclass
class OracleResult:
    """Outcome of the independent verifier. Agent cannot influence this."""

    passed: bool
    checks: list[OracleCheck] = field(default_factory=list)
    overreach: bool = False
    unexpected_files: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)


@dataclass
class ReliabilityRun:
    """Outcome of one (task, variant) execution."""

    task_id: str
    variant_id: str
    run_id: str

    status: RunStatus = "ok"
    claimed_complete: bool = False  # agent self-report (heuristic) — keep separate!
    oracle_passed: bool = False  # independent acceptance

    llm_rounds: int = 0
    tool_calls: int = 0
    permission_denials: int = 0
    files_changed: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    error: str = ""

    oracle: OracleResult | None = None
    transcript_path: str = ""
    diff_path: str = ""

    def to_dict(self) -> dict:
        # oracle_passed must reflect the actual oracle outcome, not a
        # separately-maintained flag that nothing ever sets.
        oracle_passed = self.oracle.passed if self.oracle else self.oracle_passed
        return {
            "task_id": self.task_id,
            "variant_id": self.variant_id,
            "run_id": self.run_id,
            "status": self.status,
            "claimed_complete": self.claimed_complete,
            "oracle_passed": oracle_passed,
            "llm_rounds": self.llm_rounds,
            "tool_calls": self.tool_calls,
            "permission_denials": self.permission_denials,
            "files_changed": self.files_changed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "oracle": (
                {
                    "passed": self.oracle.passed,
                    "checks": [
                        {
                            "id": c.id,
                            "command": c.command,
                            "passed": c.passed,
                            "exit_code": c.exit_code,
                            "detail": c.detail[:200],
                        }
                        for c in self.oracle.checks
                    ],
                    "overreach": self.oracle.overreach,
                    "unexpected_files": self.oracle.unexpected_files,
                    "failure_reasons": self.oracle.failure_reasons,
                }
                if self.oracle
                else None
            ),
            "transcript_path": self.transcript_path,
            "diff_path": self.diff_path,
        }
