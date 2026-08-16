"""Subagent UI events: nested scoped blocks instead of leaked logs."""

from __future__ import annotations

import io
import threading
from unittest import mock

import pytest

from harness.ui import events


@pytest.fixture
def event_sink():
    """Enable the JSONL event stream and capture emitted events."""
    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        yield sink
    finally:
        events.disable_event_stream()


def _captured(sink: io.StringIO) -> list[dict]:
    import json

    out = []
    for line in sink.getvalue().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _response(content: list[dict]):
    return mock.Mock(content=content)


def test_renderer_downgrades_unicode_for_legacy_console(monkeypatch):
    import importlib

    renderer_mod = importlib.import_module("harness.ui.renderer")

    class _GbkStdout:
        encoding = "gbk"

    monkeypatch.setattr(renderer_mod.sys, "stdout", _GbkStdout())

    assert renderer_mod._terminal_safe_text("✓ Finished") == "? Finished"


def test_run_agent_task_emits_scoped_events(event_sink):
    """A subagent run emits subagent_start → round → end, and never leaks
    tool_start / log events into the main timeline."""
    from harness.agents.runner import run_agent_task

    with mock.patch(
        "harness.agents.runner.create_message",
        return_value=_response([{"type": "text", "text": "finding: main.py handles it"}]),
    ):
        result = run_agent_task("search main", "find entry", "explore")

    assert "finding" in result
    events_list = _captured(event_sink)

    types = [e["type"] for e in events_list]
    assert "subagent_start" in types
    assert "subagent_round" in types
    assert "subagent_end" in types
    # The scoped block must not leak generic tool/log events.
    assert "tool_start" not in types
    assert "log" not in types

    start = next(e for e in events_list if e["type"] == "subagent_start")
    assert start["agent_type"] == "explore"
    assert start["description"] == "search main"
    assert start["model"]
    assert start["id"]

    end = next(e for e in events_list if e["type"] == "subagent_end")
    assert end["id"] == start["id"]
    assert end["ok"] is True
    assert end["summary"]


def test_run_agent_task_stops_while_waiting_for_a_model_response():
    from harness.agents.runner import AgentTaskStats, run_agent_task

    request_started = threading.Event()
    release_response = threading.Event()
    response_returned = threading.Event()

    def delayed_response(**kwargs):
        request_started.set()
        release_response.wait(timeout=2)
        try:
            return _response([{"type": "text", "text": "too late"}])
        finally:
            response_returned.set()

    stats = AgentTaskStats()
    try:
        with mock.patch("harness.agents.runner.create_message", side_effect=delayed_response):
            result = run_agent_task(
                "wait for model",
                "return a response",
                "explore",
                cancel_check=request_started.is_set,
                stats=stats,
            )
    finally:
        release_response.set()
        assert response_returned.wait(timeout=1)

    assert "stopped while waiting for model: cancelled" in result
    assert stats.interrupted is True
    assert stats.stop_reason == "cancelled"


def test_goal_subagent_passes_its_configured_reasoning_effort():
    from harness.agents.runner import run_agent_task

    with mock.patch(
        "harness.agents.runner.create_message",
        return_value=_response([{"type": "text", "text": "{}"}]),
    ) as create:
        run_agent_task("plan goal", "return JSON", "goal_planner")

    assert create.call_args.kwargs["model_id"] == "deepseek-v4-pro"
    assert create.call_args.kwargs["reasoning_effort"] == "max"


def test_run_agent_task_emits_nested_tools(event_sink):
    """Tool calls inside a subagent are emitted as scoped subagent_tool events
    (start ok=None, completion ok=True) — not as main-timeline tool events."""
    from harness.agents.runner import run_agent_task

    tool_round = _response(
        [
            {
                "type": "tool_use",
                "id": "t001",
                "name": "bash",
                "input": {"command": "echo hi"},
            }
        ]
    )
    final_round = _response([{"type": "text", "text": "done"}] + [
        {"type": "tool_result", "tool_use_id": "t001", "content": "hi\n"},
        {"type": "text", "text": "final answer"},
    ])
    with mock.patch(
        "harness.agents.runner.create_message",
        side_effect=[tool_round, final_round],
    ), mock.patch("harness.agents.runner.trigger_hooks", return_value=None), mock.patch(
        "harness.agents.runner.call_tool_handler", return_value="hi"
    ):
        result = run_agent_task("run probe", "run it", "explore")

    assert "final answer" in result
    events_list = _captured(event_sink)
    tool_events = [e for e in events_list if e["type"] == "subagent_tool"]
    assert len(tool_events) == 2

    start, end = tool_events
    assert start["name"] == "bash"
    assert start["ok"] is None
    assert end["name"] == "bash"
    assert end["ok"] is True
    # same run scope for the whole block
    run_ids = {e["id"] for e in events_list if e["type"].startswith("subagent_")}
    assert len(run_ids) == 1
    assert "tool_start" not in [e["type"] for e in events_list]


def test_renderer_silent_in_event_stream_mode(event_sink):
    """Classic CLI lines are suppressed while the event stream is active, so
    the TUI never sees duplicated human text."""
    from harness.ui.renderer import renderer

    renderer.subagent_start("abc123", "explore", "look around", "mimo-v2.5-pro")
    renderer.subagent_round("abc123", 1, "reading files")
    renderer.subagent_tool("abc123", "read_file", {"path": "a.md"})
    renderer.subagent_tool("abc123", "read_file", {"path": "a.md"}, "content ok")
    renderer.subagent_end("abc123", "all done", 1, 0.5)

    # _write goes nowhere in event-stream mode; only JSON events appear.
    import sys

    assert sys.stdout is not sys.__stdout__  # sanity of the fixture itself
    sink_text = event_sink.getvalue()
    assert "[task:explore]" not in sink_text
    assert sink_text.count('"type": "subagent_') == 5


def test_subagent_events_survive_disable_reset(event_sink):
    """After disabling the stream, subagent methods still work (CLI path)."""
    from harness.ui.renderer import renderer

    events.disable_event_stream()
    renderer.subagent_start("id", "explore", "desc", "model")
    # No exception means the CLI path is intact.


def test_main_loop_tool_wraps_subagent_block(event_sink):
    """The main timeline shows one `task` tool row; the subagent block nests
    between its tool_start and tool_end — nothing else leaks out."""
    from harness.agents.runner import run_agent_task
    from harness.ui.renderer import renderer

    task_input = {"description": "research X", "prompt": "find X", "agent_type": "explore"}

    # main loop calls: tool_start -> handler (subagent events) -> tool_result
    renderer.tool_start("task", task_input, tool_use_id="main-1")
    with mock.patch(
        "harness.agents.runner.create_message",
        return_value=_response([{"type": "text", "text": "summary of X"}]),
    ):
        run_agent_task("research X", "find X", "explore")
    renderer.tool_result("summary of X", name="task", tool_input=task_input, tool_use_id="main-1")

    events_list = _captured(event_sink)
    types = [e["type"] for e in events_list]
    # task row opens, subagent block runs, task row closes
    assert types[0] == "tool_start"
    assert types[-1] == "tool_end"
    assert "subagent_start" in types[1:]
    assert "subagent_end" in types[1:-1]
    assert types.index("subagent_start") < types.index("subagent_end") < types.index("tool_end")
    # only the task tool row + the subagent block; no stray logs
    assert sum(t == "log" for t in types) == 0
