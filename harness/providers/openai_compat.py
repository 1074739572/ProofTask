"""OpenAI-compatible provider with Anthropic-shaped responses for the harness."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from openai import OpenAI

from harness.providers.config import ProviderConfig, provider_timeout, resolve_api_key
from harness.providers.types import MessageResponse, TextBlock, ToolUseBlock

_lock = threading.Lock()
_clients: dict[str, OpenAI] = {}


def get_openai_client(provider: ProviderConfig) -> OpenAI:
    with _lock:
        cached = _clients.get(provider.id)
        if cached is not None:
            return cached
        api_key = resolve_api_key(provider)
        if not api_key:
            raise RuntimeError(
                f"Missing API key for provider '{provider.label}'. "
                f"Set {provider.api_key_env} in .env"
            )
        import httpx

        connect, read, write, pool = provider_timeout()
        client = OpenAI(
            api_key=api_key,
            base_url=provider.base_url,
            timeout=httpx.Timeout(read, connect=connect, write=write, pool=pool),
        )
        _clients[provider.id] = client
        return client


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_field(block: Any, name: str, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _block_text(block: Any) -> str:
    return str(_block_field(block, "text", "") or "")


def anthropic_tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return openai_tools


def anthropic_messages_to_openai(messages: list) -> list[dict]:
    openai_messages: list[dict] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "user":
            if isinstance(content, str):
                openai_messages.append({"role": "user", "content": content})
                continue
            if not isinstance(content, list):
                openai_messages.append({"role": "user", "content": str(content)})
                continue

            text_parts: list[str] = []
            for block in content:
                btype = _block_type(block)
                if btype == "text":
                    text = _block_text(block)
                    if text:
                        text_parts.append(text)
                elif btype == "tool_result":
                    if text_parts:
                        openai_messages.append(
                            {"role": "user", "content": "\n".join(text_parts)}
                        )
                        text_parts = []
                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": _block_field(block, "tool_use_id", ""),
                            "content": str(_block_field(block, "content", "")),
                        }
                    )
            if text_parts:
                openai_messages.append({"role": "user", "content": "\n".join(text_parts)})
            continue

        if role == "assistant":
            if isinstance(content, str):
                openai_messages.append({"role": "assistant", "content": content})
                continue
            if not isinstance(content, list):
                openai_messages.append({"role": "assistant", "content": str(content)})
                continue

            text_parts = []
            tool_calls = []
            for block in content:
                btype = _block_type(block)
                if btype == "text":
                    text = _block_text(block)
                    if text:
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_input = _block_field(block, "input", {}) or {}
                    tool_calls.append(
                        {
                            "id": _block_field(block, "id", f"call_{uuid.uuid4().hex[:12]}"),
                            "type": "function",
                            "function": {
                                "name": _block_field(block, "name", ""),
                                "arguments": json.dumps(tool_input, ensure_ascii=False),
                            },
                        }
                    )
            assistant_msg: dict = {"role": "assistant"}
            assistant_msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            openai_messages.append(assistant_msg)

    return openai_messages


def _map_stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    if finish_reason == "length":
        return "max_tokens"
    if has_tool_calls or finish_reason == "tool_calls":
        return "tool_use"
    return "end_turn"


def openai_response_to_anthropic(completion) -> MessageResponse:
    choice = completion.choices[0]
    message = choice.message
    blocks: list = []
    usage = getattr(completion, "usage", None)

    if message.content:
        blocks.append(TextBlock(text=message.content))

    for tool_call in message.tool_calls or []:
        raw_args = tool_call.function.arguments or "{}"
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed_args = {"raw": raw_args}
        blocks.append(
            ToolUseBlock(
                id=tool_call.id,
                name=tool_call.function.name,
                input=parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args},
            )
        )

    has_tool_calls = bool(message.tool_calls)
    return MessageResponse(
        content=blocks,
        stop_reason=_map_stop_reason(choice.finish_reason, has_tool_calls),
        model=getattr(completion, "model", None),
        usage=usage,
    )


def create_openai_message(
    *,
    provider: ProviderConfig,
    model: str,
    messages: list,
    max_tokens: int,
    system: str | None = None,
    tools: list | None = None,
) -> MessageResponse:
    client = get_openai_client(provider)
    openai_messages = anthropic_messages_to_openai(messages)
    if system:
        openai_messages = [{"role": "system", "content": system}, *openai_messages]

    kwargs: dict = {
        "model": model,
        "messages": openai_messages,
        "max_tokens": max_tokens,
    }
    openai_tools = anthropic_tools_to_openai(tools)
    if openai_tools:
        kwargs["tools"] = openai_tools

    completion = client.chat.completions.create(**kwargs)
    return openai_response_to_anthropic(completion)
