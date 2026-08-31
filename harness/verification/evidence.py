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

import re
import hashlib
import json

from harness.features.schema import VerificationEvidence
from harness.verification.runner import VerificationRunResult
from harness.verification.snapshot import capture_code_snapshot

#: Characters of stdout kept in evidence (tail is most informative).
EVIDENCE_TAIL_CHARS = 4000

#: Exit code convention for a timed-out command (matches timeout(1)).
EXIT_CODE_TIMEOUT = 124


def diagnose_verification_output(
    output: str,
    *,
    selectors: tuple[str, ...] | list[str] = (),
) -> dict:
    """Extract a concise, machine-readable explanation from test output.

    A suite-level renderer/setup exception can make every case fail before its
    assertions run.  Keep that distinction explicit so callers do not report
    ``0 pass / N fail`` as N independent behavior regressions.
    """
    text = str(output or "")
    observation_gap = None
    match = re.search(r'"classification"\s*:\s*"([^"]+)"', text)
    if match:
        observation_gap = match.group(1).strip()
    summary: dict[str, int] = {}
    pass_match = re.search(r"\b(\d+)\s+pass(?:ed)?\b", text, re.IGNORECASE)
    fail_match = re.search(r"\b(\d+)\s+fail(?:ed)?\b", text, re.IGNORECASE)
    run_match = re.search(r"\bRan\s+(\d+)\s+tests?\b", text, re.IGNORECASE)
    if pass_match:
        summary["passed"] = int(pass_match.group(1))
    if fail_match:
        summary["failed"] = int(fail_match.group(1))
    if run_match:
        summary["total"] = int(run_match.group(1))

    failed_cases: list[str] = []
    for line in text.splitlines():
        # Bun/OpenTUI: (fail) AC1 ... [1.2ms]
        match = re.match(r"\s*\(fail\)\s+(.+?)(?:\s+\[[^]]+\])?\s*$", line, re.IGNORECASE)
        if match:
            failed_cases.append(match.group(1).strip())
        # node:test: not ok 1 - case title
        match = re.match(r"\s*not ok\s+\d+\s+-\s+(.+?)\s*$", line, re.IGNORECASE)
        if match:
            failed_cases.append(match.group(1).strip())

    # Assertion errors are case-level behavior evidence. They may appear
    # alongside a renderer error, but must not be classified as a suite setup
    # blocker merely because every case failed.
    assertion_markers = bool(re.search(
        r"(?:AssertionError|assert(?:ion)?\s+(?:failed|expected)|Expected:\s*[^\r\n]+|Received:\s*[^\r\n]+)",
        text,
        re.IGNORECASE,
    ))
    common_patterns = (
        r"(?:error:\s*)?(Orphan text error:[^\r\n]+)",
        r"(SyntaxError:[^\r\n]+)",
        r"(ReferenceError:[^\r\n]+)",
        r"(TypeError:[^\r\n]+)",
        r"(ModuleNotFoundError:[^\r\n]+)",
        r"(Cannot find module[^\r\n]+)",
    )
    common_failure = None
    for pattern in common_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            common_failure = match.group(1).strip()
            break

    failed_count = summary.get("failed", 0)
    # A renderer test can time out while waiting for a frame without ever
    # reaching its behavior assertion. Keep this distinct from an assertion
    # failure so Goal repair does not modify product code on missing test
    # scheduler/event-delivery evidence.
    frame_wait_timeout = bool(re.search(
        r"(?:Timed out waiting for frame predicate|Timed out waiting for visual idle|frame predicate)",
        text,
        re.IGNORECASE,
    ))
    setup_markers = bool(re.search(
        r"(?:Orphan text error|SyntaxError|ReferenceError|TypeError|ModuleNotFoundError|Cannot find module|failed to load|test suite failed|beforeAll.*failed)",
        text,
        re.IGNORECASE,
    ))
    blocked = bool(
        setup_markers
        and not assertion_markers
        and failed_count > 0
        and summary.get("passed", 0) == 0
    )
    expected_actual: dict[str, str] = {}
    expected = re.search(r"\bExpected:\s*([^\r\n]+)", text, re.IGNORECASE)
    received = re.search(r"\bReceived:\s*([^\r\n]+)", text, re.IGNORECASE)
    if expected and received:
        expected_actual = {
            "expected": expected.group(1).strip()[:500],
            "actual": received.group(1).strip()[:500],
        }
    passed_count = summary.get("passed", 0)
    observed_total = summary.get("total")
    # A successful command may print a suite summary plus unrelated warnings;
    # the machine verdict is still clean when Bun reports no failed tests.
    clean_success = passed_count > 0 and failed_count == 0 and not common_failure
    signature_payload = {
        "failure_mode": "passed" if clean_success else ("common_runtime_error" if blocked else (
            "event_delivery_timeout" if frame_wait_timeout and not assertion_markers else (
            "case_assertions" if failed_count else ("command_error" if text else "unknown")
            )
        )),
        "common_failure": common_failure or "",
        "failed_cases": list(dict.fromkeys(failed_cases))[:16],
        "result_summary": summary,
        "expected_actual": expected_actual,
    }
    if observation_gap in {"frame_not_observable", "test_renderer_observation_gap"}:
        signature_payload["failure_mode"] = "test_observation_gap"
    failure_signature = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    failure_mode = signature_payload["failure_mode"]
    next_machine_check = {
        "common_runtime_error": "Run renderer/setup probe and verify event delivery before changing product code.",
        "event_delivery_timeout": "Run event-delivery and frame-observation probe with the bound selector.",
        "test_observation_gap": "Run renderer observation probe and confirm the updated frame is exposed.",
        "case_assertions": "Rerun the bound assertion after one scoped implementation hypothesis.",
        "command_error": "Run command/setup probe and confirm the verification process initialized.",
    }.get(failure_mode, "Rerun the bound verification command and capture a fresh evidence snapshot.")
    phase_trace = {
        "command_started": bool(text),
        "tests_collected": observed_total is not None or bool(summary),
        "setup_initialized": not bool(common_failure and blocked),
        "assertion_executed": assertion_markers or bool(failed_cases),
    }
    diagnostics = {
        "result_summary": summary,
        "failed_cases": list(dict.fromkeys(failed_cases)),
        "common_failure": common_failure,
        "failure_mode": failure_mode,
        "next_machine_check": next_machine_check,
        "phase_trace": phase_trace,
        "failure_signature": failure_signature,
        "expected_actual": expected_actual,
        "recheck_required": bool(failed_count or common_failure) and failure_mode != "passed",
        "recheck": {
            "objective": (
                "Confirm whether the failure occurred before assertions, in event/state delivery, "
                "or in rendered output before changing implementation code."
            ),
            "checks": [
                "Compare expected markers with the captured output tail.",
                "Check the bound command, selectors, and execution workspace.",
                "Use the failure signature to distinguish a new failure from a repeated one.",
            ],
        },
        "blocked_before_assertions": blocked or (frame_wait_timeout and not assertion_markers),
        "event_delivery_timeout": frame_wait_timeout and not assertion_markers,
        "test_observation_gap": failure_mode == "test_observation_gap",
        "observation_classification": observation_gap,
        "assertion_markers_present": assertion_markers,
        "observed_count": observed_total,
    }
    if blocked and selectors:
        diagnostics["affected_selectors"] = [str(selector) for selector in selectors]
    return diagnostics


def evidence_from_result(
    result: VerificationRunResult,
    *,
    workspace: str | None = None,
    verified_by: str = "runner",
    selectors: tuple[str, ...] | list[str] = (),
    collected_count: int = 0,
) -> VerificationEvidence:
    """Build evidence from a run result. Timeouts get exit code 124."""
    if result.timed_out:
        exit_code = EXIT_CODE_TIMEOUT
    else:
        exit_code = result.exit_code if result.exit_code is not None else EXIT_CODE_TIMEOUT
    tail = (result.stdout or "")[-EVIDENCE_TAIL_CHARS:]
    diagnostics = diagnose_verification_output(result.stdout or "", selectors=selectors)
    diagnostics["verified_workspace"] = str(workspace or "")
    diagnostics["verified_snapshot"] = capture_code_snapshot(workspace)
    return VerificationEvidence(
        command=result.command,
        exit_code=exit_code,
        stdout_tail=tail,
        duration_ms=round(result.duration_ms, 1),
        verified_by=verified_by,
        code_snapshot=diagnostics["verified_snapshot"],
        selectors=tuple(str(selector) for selector in selectors),
        collected_count=max(0, int(collected_count)),
        diagnostics=diagnostics,
    )
