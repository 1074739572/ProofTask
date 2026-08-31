from harness.verification.evidence import diagnose_verification_output, evidence_from_result
from harness.verification.runner import VerificationRunResult


def test_diagnose_common_runtime_failure_marks_all_cases_blocked():
    output = """error: Orphan text error: \"\" must have a <text> as a parent
(fail) AC1 renders summary [1.0ms]
(fail) AC2 renders details [1.1ms]
0 pass
2 fail
Ran 2 tests across 1 file.
"""

    diagnostics = diagnose_verification_output(
        output,
        selectors=["test/goal.test.tsx::AC1", "test/goal.test.tsx::AC2"],
    )

    assert diagnostics["result_summary"] == {"passed": 0, "failed": 2, "total": 2}
    assert diagnostics["failure_mode"] == "common_runtime_error"
    assert diagnostics["blocked_before_assertions"] is True
    assert diagnostics["recheck_required"] is True
    assert len(diagnostics["failure_signature"]) == 16
    assert "execution workspace" in " ".join(diagnostics["recheck"]["checks"])
    assert diagnostics["common_failure"].startswith("Orphan text error:")
    assert diagnostics["failed_cases"] == ["AC1 renders summary", "AC2 renders details"]
    assert diagnostics["affected_selectors"] == [
        "test/goal.test.tsx::AC1",
        "test/goal.test.tsx::AC2",
    ]


def test_diagnose_independent_case_failures_does_not_call_them_common_blockers():
    diagnostics = diagnose_verification_output(
        "(fail) AC2 rejects empty input [1ms]\n1 pass\n1 fail\nRan 2 tests across 1 file.",
    )

    assert diagnostics["failure_mode"] == "case_assertions"
    assert diagnostics["blocked_before_assertions"] is False
    assert diagnostics["common_failure"] is None


def test_diagnose_frame_wait_timeout_is_not_an_implementation_failure():
    diagnostics = diagnose_verification_output(
        "error: Timed out waiting for frame predicate after 20 passes\n"
        "lastFrame: BEFORE\n0 pass\n1 fail\nRan 1 test across 1 file."
    )

    assert diagnostics["failure_mode"] == "event_delivery_timeout"
    assert diagnostics["event_delivery_timeout"] is True
    assert diagnostics["blocked_before_assertions"] is True
    assert diagnostics["expected_actual"] == {}


def test_diagnose_component_trace_routes_renderer_observation_gap_to_contract_review():
    diagnostics = diagnose_verification_output(
        '{"interaction_trace":[{"event":"state_after","target":"GOAL_DETAILS_TOGGLE"}],"classification":"frame_not_observable"}\n'
        "0 pass\n1 fail\nRan 1 test across 1 file."
    )
    assert diagnostics["failure_mode"] == "test_observation_gap"
    assert diagnostics["test_observation_gap"] is True
    assert diagnostics["observation_classification"] == "frame_not_observable"


def test_diagnose_assertion_error_with_zero_pass_is_case_failure():
    diagnostics = diagnose_verification_output(
        "(fail) OV4 keeps siblings on the same row [1ms]\n"
        "AssertionError: expected 0 to be 1\n"
        "Expected: 0\nReceived: 1\n"
        "0 pass\n1 fail\nRan 1 test across 1 file."
    )

    assert diagnostics["failure_mode"] == "case_assertions"
    assert diagnostics["blocked_before_assertions"] is False
    assert diagnostics["assertion_markers_present"] is True
    assert diagnostics["expected_actual"] == {"expected": "0", "actual": "1"}


def test_diagnose_clean_success_is_not_command_error():
    diagnostics = diagnose_verification_output(
        "5 pass\n0 fail\nRan 5 tests across 1 file."
    )

    assert diagnostics["failure_mode"] == "passed"
    assert diagnostics["recheck_required"] is False
    assert diagnostics["observed_count"] == 5


def test_evidence_persists_diagnostics_without_changing_verdict():
    result = VerificationRunResult(
        command="bun test test/goal.test.tsx",
        exit_code=1,
        stdout="Orphan text error: \"\" must have a <text> as a parent\n0 pass\n5 fail\nRan 5 tests across 1 file.",
        timed_out=False,
        duration_ms=12,
    )

    evidence = evidence_from_result(
        result,
        selectors=["test/goal.test.tsx::AC1"],
        collected_count=1,
    ).to_dict()

    assert evidence["exit_code"] == 1
    assert evidence["diagnostics"]["failure_mode"] == "common_runtime_error"
    assert evidence["diagnostics"]["blocked_before_assertions"] is True
    assert evidence["diagnostics"]["recheck_required"] is True
    assert evidence["diagnostics"]["verified_workspace"] == ""
