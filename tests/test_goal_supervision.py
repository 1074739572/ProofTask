from __future__ import annotations

from harness.agents.runner import AgentTaskStats
from harness.goal.supervision import StagePolicy, StageProgress, StageSupervisor


def test_stage_supervisor_continues_only_after_observable_progress():
    artifacts: list[str] = []
    calls: list[tuple[str, str]] = []

    def invoke(prompt, description, _slice, stats):
        calls.append((prompt, description))
        if len(calls) == 1:
            artifacts.append("tests/test_new.py")
            stats.llm_rounds = 8
            stats.stop_reason = "max_rounds"
            return "still inspecting"
        return '{"test_selectors":["tests/test_new.py::test_new"]}'

    result = StageSupervisor(StagePolicy("tests", slice_rounds=8, max_idle_slices=2)).run(
        invoke=invoke,
        initial_prompt="write a test",
        initial_description="write test",
        continuation_prompt=lambda _slice, progress, _idle: f"continue: {progress.summary}",
        continuation_description=lambda slice: f"continue {slice}",
        snapshot=lambda: tuple(artifacts),
        assess_progress=lambda before, after, _stats: StageProgress(
            advanced=before != after,
            summary="new test artifact" if before != after else "no artifact",
            checkpoint={"artifacts": list(after)},
        ),
    )

    assert result.slices == 2
    assert result.stalled is False
    assert "new test artifact" in calls[1][0]
    assert result.checkpoint == {"artifacts": ["tests/test_new.py"]}


def test_stage_supervisor_stops_after_configured_idle_slices():
    calls = []

    def invoke(_prompt, _description, _slice, stats):
        calls.append(1)
        stats.stop_reason = "max_rounds"
        return "still reading the same files"

    result = StageSupervisor(StagePolicy("discovery", slice_rounds=8, max_idle_slices=2)).run(
        invoke=invoke,
        initial_prompt="discover",
        initial_description="discover",
        continuation_prompt=lambda *_: "redirect",
        continuation_description=lambda slice: f"continue {slice}",
        snapshot=lambda: (),
        assess_progress=lambda *_: StageProgress(False, "no new evidence"),
    )

    assert len(calls) == 2
    assert result.stalled is True
    assert result.idle_slices == 2


def test_stage_supervisor_continues_after_max_tokens():
    calls = []

    def invoke(prompt, _description, _slice, stats):
        calls.append(prompt)
        if len(calls) == 1:
            stats.stop_reason = "max_tokens"
            return "partial response"
        return '{"evidence": []}'

    result = StageSupervisor(StagePolicy("discovery", slice_rounds=8, max_idle_slices=2)).run(
        invoke=invoke,
        initial_prompt="discover",
        initial_description="discover",
        continuation_prompt=lambda *_: "continue the truncated response",
        continuation_description=lambda slice: f"continue {slice}",
        snapshot=lambda: (),
        assess_progress=lambda *_: StageProgress(False, "no new evidence yet"),
    )

    assert calls == ["discover", "continue the truncated response"]
    assert result.slices == 2
    assert result.stalled is False
    assert result.raw == '{"evidence": []}'


def test_stage_supervisor_stalls_after_repeated_max_token_slices_without_progress():
    calls = []

    def invoke(_prompt, _description, _slice, stats):
        calls.append(1)
        stats.stop_reason = "max_tokens"
        return "partial response"

    result = StageSupervisor(StagePolicy("tests", slice_rounds=8, max_idle_slices=2)).run(
        invoke=invoke,
        initial_prompt="write tests",
        initial_description="write tests",
        continuation_prompt=lambda *_: "continue",
        continuation_description=lambda slice: f"continue {slice}",
        snapshot=lambda: (),
        assess_progress=lambda *_: StageProgress(False, "no test artifact"),
    )

    assert len(calls) == 2
    assert result.stalled is True
    assert result.stats.stop_reason == "max_tokens"


def test_stage_supervisor_rejects_completed_output_without_required_artifact():
    artifacts: list[str] = []
    calls = []

    def invoke(prompt, _description, _slice, stats):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"test_selectors": []}'
        artifacts.append("tests/test_new.py")
        return '{"test_selectors": ["tests/test_new.py::test_new"]}'

    result = StageSupervisor(StagePolicy("tests", slice_rounds=8, max_idle_slices=3)).run(
        invoke=invoke,
        initial_prompt="write a test",
        initial_description="write test",
        continuation_prompt=lambda *_: "the required test artifact is still missing",
        continuation_description=lambda slice: f"continue {slice}",
        snapshot=lambda: tuple(artifacts),
        assess_progress=lambda before, after, _stats: StageProgress(
            advanced=before != after,
            summary="new test artifact" if before != after else "no test artifact",
        ),
        continue_when=lambda _raw, stats, progress: (
            stats.stop_reason == "completed" and not progress.advanced
        ),
    )

    assert len(calls) == 2
    assert calls[1] == "the required test artifact is still missing"
    assert result.stalled is False
    assert result.checkpoint == {}


def test_agent_task_stats_records_paths_and_tool_failures():
    stats = AgentTaskStats()

    stats.record_tool("read_file", {"path": "src\\app.py"}, "contents")
    stats.record_tool("edit_file", {"path": "tests/test_app.py"}, "Write blocked: test-only")

    assert stats.tool_count == 2
    assert stats.read_paths == ["src/app.py"]
    assert stats.write_paths == ["tests/test_app.py"]
    assert stats.tool_errors == ["edit_file: Write blocked: test-only"]
