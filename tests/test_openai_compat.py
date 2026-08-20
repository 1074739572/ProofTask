"""Response normalization for OpenAI-compatible reasoning providers."""

from types import SimpleNamespace

from harness.providers.openai_compat import openai_response_to_anthropic
from harness.tools.dispatch import extract_text


def _completion(*, content=None, reasoning_content=None, tool_calls=None):
    message = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls or [],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="deepseek-v4-pro",
        usage=None,
    )


def test_reasoning_content_falls_back_when_final_content_is_empty():
    response = openai_response_to_anthropic(
        _completion(reasoning_content='{"questions":[]}')
    )

    assert extract_text(response.content) == '{"questions":[]}'


def test_visible_content_wins_over_reasoning_content():
    response = openai_response_to_anthropic(
        _completion(content="final answer", reasoning_content="internal reasoning")
    )

    assert extract_text(response.content) == "final answer"


def test_stream_reasoning_content_falls_back_when_content_is_empty(monkeypatch):
    from harness.providers import openai_compat

    class Delta:
        content = None
        reasoning_content = '{"questions":[]}'
        reasoning = None
        tool_calls = []

    class Choice:
        delta = Delta()
        finish_reason = "stop"

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**_kwargs):
                    return iter([type("Chunk", (), {"choices": [Choice()], "model": "deepseek-v4-pro", "usage": None})()])

    deltas = []
    response = openai_compat._stream_openai(Client(), {}, lambda *args: deltas.append(args))

    assert extract_text(response.content) == '{"questions":[]}'
    assert deltas == []
