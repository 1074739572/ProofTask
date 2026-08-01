"""Route LLM calls to the correct provider backend."""

from __future__ import annotations

from harness.models import ModelProfile
from harness.project.session import messages_for_api
from harness.providers.anthropic import get_anthropic_client
from harness.providers.config import get_provider
from harness.providers.openai_compat import create_openai_message


def create_provider_message(
    *,
    profile: ModelProfile,
    messages: list,
    max_tokens: int,
    system: str | None = None,
    tools: list | None = None,
    on_delta: callable = None,
):
    provider = get_provider(profile.provider)
    # Session blocks may include provider-specific shapes; sanitize before API.
    api_messages = messages_for_api(messages)

    if provider.type == "anthropic":
        client = get_anthropic_client(provider)
        kwargs: dict = {
            "model": profile.api_model,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools
        if profile.thinking:
            kwargs["thinking"] = {"type": "enabled"}
        return client.messages.create(**kwargs)

    if provider.type == "openai":
        if profile.thinking:
            print("  \033[33m[llm] thinking mode ignored for OpenAI-compatible provider\033[0m")
        return create_openai_message(
            provider=provider,
            model=profile.api_model,
            messages=api_messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            reasoning_effort=profile.reasoning_effort,
            extra_body=profile.extra_body,
            on_delta=on_delta,
        )

    raise RuntimeError(f"Unsupported provider type '{provider.type}' for '{provider.id}'")
