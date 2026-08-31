from harness.verification.debug import (
    build_debug_bundle,
    extract_stack_frames,
    lint_test_contract,
)
from harness.verification.runner import VerificationRunResult


def test_extract_stack_frames_and_source_context(tmp_path):
    source = tmp_path / "src" / "OverlayLayer.tsx"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {i}" for i in range(1, 20)), encoding="utf-8")
    output = f'Error: Orphan text error: "" must have a <text> as a parent\n    at render ({source}:10:8)\n0 pass\n1 fail\nRan 1 test across 1 file.'

    bundle = build_debug_bundle(
        VerificationRunResult("bun test test/overlay.test.tsx", 1, output, False, 12.5),
        workspace=tmp_path,
        task_id="task-1",
        selectors=["test/overlay.test.tsx::OF1"],
        test_files=[],
    )

    assert bundle["failure"]["category"] == "runtime_render"
    assert bundle["failure"]["signature"]
    assert bundle["stack"][0]["line"] == 10
    assert bundle["source_context"][0]["path"] == "src/OverlayLayer.tsx"
    assert any("10: line 10" in line for line in bundle["source_context"][0]["context"])


def test_assertion_failure_keeps_expected_actual_and_is_not_runtime_blocker(tmp_path):
    output = "(fail) OF1 keeps layout [1ms]\nAssertionError: mismatch\nExpected: 0\nReceived: 1\n0 pass\n1 fail\nRan 1 test across 1 file."
    bundle = build_debug_bundle(
        VerificationRunResult("bun test test/overlay.test.tsx", 1, output, False, 2),
        workspace=tmp_path,
        selectors=["test/overlay.test.tsx::OF1"],
    )

    assert bundle["failure"]["category"] == "behavior_assertion"
    assert bundle["failure"]["expected_actual"] == {"expected": "0", "actual": "1"}
    assert bundle["diagnostics"]["blocked_before_assertions"] is False


def test_lint_detects_visible_option_with_no_border_assertion(tmp_path):
    test_file = tmp_path / "test" / "overlay.test.tsx"
    test_file.parent.mkdir()
    test_file.write_text(
        """
import test from 'node:test';
test('R2 optional fields', () => {
  const VISIBLE_OPTION = 'visible';
  assert.match(frame, /VISIBLE_OPTION/);
  assert.doesNotMatch(frame, BORDER);
});
""",
        encoding="utf-8",
    )

    report = lint_test_contract(
        ["test/overlay.test.tsx"],
        workspace=tmp_path,
        acceptance_ids=["OF1"],
    )

    assert report["invalid"] is True
    codes = {item["code"] for item in report["issues"]}
    assert "contradictory_visibility_assertion" in codes
    assert "acceptance_case_mismatch" in codes


def test_lint_detects_marker_reused_as_current_task_subject(tmp_path):
    test_file = tmp_path / "test" / "goal-details.test.tsx"
    test_file.parent.mkdir()
    test_file.write_text(
        """
const TASK_GRAPH = 'TASK_GRAPH';
const goal = {tasks: [{subject: TASK_GRAPH}]};
test('AC2 details toggle', () => {
  assert.doesNotMatch(collapsed, new RegExp(TASK_GRAPH));
});
""",
        encoding="utf-8",
    )

    report = lint_test_contract(
        ["test/goal-details.test.tsx"],
        workspace=tmp_path,
        acceptance_ids=["AC2"],
    )

    assert report["invalid"] is True
    assert "hidden_marker_is_summary_subject" in {item["code"] for item in report["issues"]}


def test_bundle_upgrades_contract_contradiction_over_generic_runtime_error(tmp_path):
    test_file = tmp_path / "test" / "overlay.test.tsx"
    test_file.parent.mkdir()
    test_file.write_text(
        """
import test from 'node:test';
test('R2 optional fields', () => {
  const VISIBLE_OPTION = 'visible';
  assert.doesNotMatch(frame, BORDER);
});
""",
        encoding="utf-8",
    )
    output = f'Orphan text error: "" must have a <text> as a parent\n    at render ({test_file}:6:3)\n0 pass\n1 fail\nRan 1 test across 1 file.'
    bundle = build_debug_bundle(
        VerificationRunResult("bun test test/overlay.test.tsx", 1, output, False, 3),
        workspace=tmp_path,
        selectors=["test/overlay.test.tsx::R2 optional fields"],
        test_files=["test/overlay.test.tsx"],
        acceptance_ids=["OF1"],
    )

    assert bundle["failure"]["category"] == "invalid_verification_contract"
    assert bundle["contract"]["status"] == "invalid_verification_contract"


def test_task_verification_evidence_contains_debug_bundle(tmp_path, monkeypatch):
    import harness.verification as verification

    class Task:
        id = "task-debug"
        goal_id = "goal-debug"
        acceptance_cases = [{"id": "OF1"}]
        primary_write = ["src-open/OverlayLayer.tsx"]
        planned_new = []
        verification_spec = {
            "command": "bun test test/overlay.test.tsx",
            "selectors": ["test/overlay.test.tsx::OF1"],
            "test_files": ["test/overlay.test.tsx"],
            "collected_count": 1,
        }

    monkeypatch.setattr(
        verification,
        "run_verification",
        lambda *args, **kwargs: VerificationRunResult(
            "bun test test/overlay.test.tsx", 1,
            "Orphan text error: empty node\n0 pass\n1 fail\nRan 1 test across 1 file.",
            False, 1.0,
        ),
    )
    passed, evidence, error = verification._run_task_verification(Task(), workspace=tmp_path, controller_authorized=True)

    assert passed is False
    assert error.startswith("verification blocked before assertions:")
    assert evidence is not None
    paths = evidence["diagnostics"]["debug_bundle"]
    assert (tmp_path / ".project" / "verification-debug").is_dir()
    assert paths["json"].endswith("bundle.json")
