"""Mechanism evals (M-series): verify harness MECHANISMS actually work end-to-end.

Unlike H-series (task completion), M-series asserts that a specific harness
mechanism behaves correctly in a REAL run:
- M001: bash tool does not hang when the parent stdin is a pipe (TUI case)
- M003: typed subagents inherit the OS/shell environment hint
- M004: global vs project handbook precedence (depends on global layer)

Workflow per mechanism: write the eval FIRST (it fails on the buggy code),
then fix the harness, then the same eval goes green. Both results are recorded
in docs/harness-reliability-plan.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MResult:
    id: str
    name: str
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0
    runs: int = 1
    attempts: list[bool] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 1),
            "runs": self.runs,
            "attempts": self.attempts,
        }


def summarize(results: list[MResult]) -> dict:
    return {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [r.to_dict() for r in results],
    }
