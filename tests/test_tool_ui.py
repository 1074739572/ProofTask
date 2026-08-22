"""Tool UI: steps + changed files; no HARNESS_TOOL_UI mode matrix."""

from harness.ui.tool_display import is_failure_tool_output, summarize_failure_output
from harness.ui.turn_summary import TurnMutationTracker, mutated_path_from_tool


def test_success_outputs_are_not_failures():
    assert is_failure_tool_output("22 lines · Title: …") is False
    assert is_failure_tool_output("Wrote 963 bytes to check_affil.py") is False
    assert is_failure_tool_output("ok (no output)") is False


def test_errors_and_guards_are_failures():
    assert is_failure_tool_output("Error: Timeout (120s)") is True
    assert is_failure_tool_output("[LookupGuard] Blocked: …") is True
    assert is_failure_tool_output("[GroundingGuard] Blocked") is True
    assert "blocked duplicate" in summarize_failure_output(
        "[RepeatGuard] Blocked identical call"
    )


def test_mutation_tracker_records_writes():
    tracker = TurnMutationTracker()
    tracker.note("write_file", {"path": "a.py"}, "Wrote 10 bytes to a.py")
    tracker.note("edit_file", {"path": "b.py"}, "Edited b.py")
    tracker.note("patch_file", {"path": "c.py", "hunks": [{}]}, "Patched c.py")
    tracker.note("bash", {"command": "ls"}, "ok")
    tracker.note("write_file", {"path": "a.py"}, "Wrote again")
    tracker.note("write_file", {"path": "c.py"}, "Error: Permission denied")
    assert tracker.paths == ["a.py", "b.py", "c.py"]
    assert mutated_path_from_tool("read_file", {"path": "x"}, "hi") is None
