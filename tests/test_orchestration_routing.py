"""Regression tests for role-bound multi-agent orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent.recovery import RecoveryState
from harness.loop import call_llm
from harness.modes import routing

ROOT = Path(__file__).resolve().parent.parent


def _config(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def test_orchestrate_role_bindings_and_permissions() -> None:
    agents = _config("agents.json")["agents"]
    modes = _config("modes.json")["modes"]
    models = {entry["id"]: entry for entry in _config("models.json")["models"]}
    providers = _config("providers.json")

    assert modes["orchestrate"]["lead_model_hint"] == "gpt-5.6-sol"
    assert agents["explore"]["model_id"] == "mimo-v2.5-pro"
    assert set(agents["explore"]["tools"]) <= {"bash", "read_file", "glob", "search_text"}
    assert not ({"write_file", "edit_file"} & set(agents["explore"]["tools"]))
    assert "bash" not in agents["goal_worker"]["tools"]
    assert models["mimo-v2.5-pro"]["provider"] == "xiaomi-mimo"
    assert providers["xiaomi-mimo"]["type"] == "openai"

    assert agents["code"]["model_id"] == "deepseek-v4-flash"
    assert agents["code"]["reasoning_effort"] == "max"
    assert {"write_file", "edit_file"} <= set(agents["code"]["tools"])
    assert models["deepseek-v4-flash"]["provider"] == "opencode"
    assert models["deepseek-v4-pro"]["provider"] == "deepseek"


def _call_with_model_selection(*, task_enabled: bool, fallback: str | None = None):
    response = SimpleNamespace(content=[], stop_reason="end_turn")
    state = RecoveryState()
    state.fallback_model = fallback
    with (
        patch("harness.loop.mode_enables_task", return_value=task_enabled),
        patch("harness.loop.mode_lead_model_hint", return_value="gpt-5.6-sol"),
        patch("harness.loop.get_model", return_value="deepseek-v4-flash"),
        patch("harness.loop.assemble_system_prompt", return_value="system"),
        patch("harness.loop.create_message", return_value=response) as create,
    ):
        result = call_llm([], {}, [], state, 1000)
    return result, response, create.call_args.kwargs["model_id"]


def test_call_llm_uses_mode_bound_lead_model() -> None:
    result, response, model_id = _call_with_model_selection(task_enabled=True)
    assert result is response
    assert model_id == "gpt-5.6-sol"


def test_non_orchestrate_mode_keeps_current_model() -> None:
    _, _, model_id = _call_with_model_selection(task_enabled=False)
    assert model_id == "deepseek-v4-flash"


def test_call_llm_keeps_the_literal_user_message_clean() -> None:
    response = SimpleNamespace(content=[], stop_reason="end_turn")
    messages = [{"role": "user", "content": "Explain this error in plain Chinese."}]
    with (
        patch("harness.loop.mode_enables_task", return_value=False),
        patch("harness.loop.assemble_system_prompt", return_value="runtime system") as assemble,
        patch("harness.loop.get_model", return_value="deepseek-v4-flash"),
        patch("harness.loop.create_message", return_value=response) as create,
    ):
        assert call_llm(messages, {}, [], RecoveryState(), 1000) is response

    assert create.call_args.kwargs["system"] == "runtime system"
    assert messages == [{"role": "user", "content": "Explain this error in plain Chinese."}]
    assert create.call_args.kwargs["messages"] == messages
    assemble.assert_called_once_with({}, base_system=None)


def test_recovery_fallback_overrides_mode_bound_lead_model() -> None:
    _, _, model_id = _call_with_model_selection(
        task_enabled=True, fallback="deepseek-v4-flash"
    )
    assert model_id == "deepseek-v4-flash"


def test_explicit_agent_model_does_not_inherit_interactive_effort() -> None:
    from harness.models import get_model_profile, initialize_model, set_reasoning_effort

    initialize_model("deepseek-v4-flash")
    set_reasoning_effort("max")
    try:
        mimo = get_model_profile("mimo-v2.5-pro")
        assert mimo.reasoning_effort is None
    finally:
        set_reasoning_effort(None)


def test_goal_calls_disable_interactive_effort_inheritance() -> None:
    from harness.models import get_model_profile, initialize_model, set_reasoning_effort

    initialize_model("mimo-v2.5-pro")
    set_reasoning_effort("max")
    try:
        mimo = get_model_profile("mimo-v2.5-pro", inherit_interactive_effort=False)
        assert mimo.reasoning_effort is None
    finally:
        set_reasoning_effort(None)


def _route_messages() -> list[dict]:
    return [{"role": "user", "content": "分析项目结构"}]


def test_route_injects_worker_result_and_preserves_other_messages() -> None:
    messages = _route_messages()
    inboxes = iter(
        [
            [
                {"from": "other", "type": "result", "content": "other result"},
                {"from": "route_explore_123", "type": "progress", "content": "started"},
                {"from": "route_explore_123", "type": "result", "content": "found entrypoint"},
            ]
        ]
    )
    with (
        patch("harness.modes.routing.mode_auto_route", return_value=True),
        patch("harness.modes.routing._teammate_name", return_value="route_explore_123"),
        patch("harness.modes.routing.spawn_teammate_thread"),
        patch("harness.modes.routing.BUS.read_inbox", side_effect=lambda _agent: next(inboxes)),
    ):
        assert routing.route_user_message(messages) is True

    assert "found entrypoint" in messages[-2]["content"]
    assert "From other [result]: other result" in messages[-1]["content"]


def test_route_injects_worker_error_without_waiting_for_timeout() -> None:
    messages = _route_messages()
    with (
        patch("harness.modes.routing.mode_auto_route", return_value=True),
        patch("harness.modes.routing._teammate_name", return_value="route_explore_123"),
        patch("harness.modes.routing.spawn_teammate_thread"),
        patch(
            "harness.modes.routing.BUS.read_inbox",
            return_value=[
                {
                    "from": "route_explore_123",
                    "type": "error",
                    "content": "LLM call failed: TimeoutError",
                }
            ],
        ),
    ):
        assert routing.route_user_message(messages) is True

    assert "ended with error" in messages[-1]["content"]
    assert "TimeoutError" in messages[-1]["content"]


def test_route_requests_shutdown_after_idle_deadline() -> None:
    messages = _route_messages()
    clock = iter([0.0, 0.0, 181.0, 181.0])
    with (
        patch("harness.modes.routing.mode_auto_route", return_value=True),
        patch("harness.modes.routing._teammate_name", return_value="route_explore_123"),
        patch("harness.modes.routing.spawn_teammate_thread"),
        patch("harness.modes.routing.BUS.read_inbox", return_value=[]),
        patch("harness.modes.routing.run_request_shutdown") as shutdown,
        patch("harness.modes.routing.time.monotonic", side_effect=lambda: next(clock)),
        patch("harness.modes.routing.ROUTE_IDLE_TIMEOUT", 180),
        patch("harness.modes.routing.ROUTE_MAX_RUNTIME", 600),
    ):
        assert routing.route_user_message(messages) is True

    shutdown.assert_called_once_with("route_explore_123")
    assert "shutdown was requested" in messages[-1]["content"]


def test_generic_question_is_not_auto_routed() -> None:
    assert routing._classify_message("为什么这个函数很慢？") is None


def test_concrete_evidence_question_can_still_route() -> None:
    assert routing._classify_message("分析项目结构，找出入口文件") == "explore"
