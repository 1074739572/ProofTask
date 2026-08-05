"""Evidence construction for verification runs (L3).

Turns a :class:`VerificationRunResult` into a :class:`VerificationEvidence`
that the feature store appends — the proof attached to a passing/failing
verdict. Output is trimmed to a fixed tail so evidence stays small even when
the command printed a lot. The code snapshot (git HEAD + worktree fingerprint)
is captured AFTER the run: for a passing verdict it must match the code that
was verified, and the clean/gate layers compare it with the live workspace to
detect stale passing states.
"""

from __future__ import annotations

from harness.features.schema import VerificationEvidence
from harness.verification.runner import VerificationRunResult
from harness.verification.snapshot import capture_code_snapshot

#: Characters of stdout kept in evidence (tail is most informative).
EVIDENCE_TAIL_CHARS = 4000

#: Exit code convention for a timed-out command (matches timeout(1)).
EXIT_CODE_TIMEOUT = 124


def evidence_from_result(
    result: VerificationRunResult,
    *,
    workspace: str | None = None,
    verified_by: str = "runner",
) -> VerificationEvidence:
    """Build evidence from a run result. Timeouts get exit code 124."""
    if result.timed_out:
        exit_code = EXIT_CODE_TIMEOUT
    else:
        exit_code = result.exit_code if result.exit_code is not None else EXIT_CODE_TIMEOUT
    tail = (result.stdout or "")[-EVIDENCE_TAIL_CHARS:]
    return VerificationEvidence(
        command=result.command,
        exit_code=exit_code,
        stdout_tail=tail,
        duration_ms=round(result.duration_ms, 1),
        verified_by=verified_by,
        code_snapshot=capture_code_snapshot(workspace),
    )
