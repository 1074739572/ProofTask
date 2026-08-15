"""OpenAI-compatible provider with Anthropic-shaped responses for the harness."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from typing import TYPE_CHECKING, Any

from harness.providers.config import ProviderConfig, provider_timeout, resolve_api_key
from harness.providers.types import MessageResponse, TextBlock, ToolUseBlock

if TYPE_CHECKING:
    from openai import OpenAI

_lock = threading.Lock()
_clients: dict[str, "OpenAI"] = {}


def get_openai_client(provider: ProviderConfig) -> "OpenAI":
    with _lock:
        cached = _clients.get(provider.id)
        if cached is not None:
            return cached
        # Lazy import: the OpenAI SDK is ~5s to import on Windows; only pay
        # that cost when a turn actually needs an OpenAI-compatible client.
        import httpx
        from openai import OpenAI

        api_key = resolve_api_key(provider)
        if not api_key:
            raise RuntimeError(
                f"Missing API key for provider '{provider.label}'. "
                f"Set {provider.api_key_env} in .env"
            )
        connect, read, write, pool = provider_timeout()
        client = OpenAI(
            api_key=api_key,
            base_url=provider.base_url,
            timeout=httpx.Timeout(read, connect=connect, write=write, pool=pool),
            # A harness turn owns retries and error reporting. SDK retries can
            # turn one 90s stalled request into several silent minutes.
            max_retries=0,
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
    reasoning_effort: str | None = None,
    extra_body: dict | None = None,
    on_delta: callable = None,
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
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if extra_body:
        kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), **extra_body}
    openai_tools = anthropic_tools_to_openai(tools)
    if openai_tools:
        kwargs["tools"] = openai_tools

    if on_delta is not None:
        kwargs["stream"] = True
        try:
            return _stream_openai(client, kwargs, on_delta)
        except Exception as exc:
            # Some OpenAI-compatible relays (e.g. 鱼鱼API / aijws) return
            # "Upstream service temporarily unavailable" for streaming while
            # non-stream works fine. Fall back to a single non-streaming
            # request so the turn still completes. Safe because such failures
            # occur before any chunk is emitted.
            if not _is_stream_recoverable(exc):
                raise
            print(
                f"  \033[33m[llm] stream unstable ({exc}); retrying non-stream\033[0m",
                file=sys.stderr,
            )
            kwargs.pop("stream", None)
            completion = client.chat.completions.create(**kwargs)
            resp = openai_response_to_anthropic(completion)
            # replay text to the TUI so output is still visible
            for _block in resp.content:
                _txt = getattr(_block, "text", None)
                if _txt:
                    on_delta(_txt, "text_delta")
            return resp

    completion = client.chat.completions.create(**kwargs)
    return openai_response_to_anthropic(completion)


def _is_stream_recoverable(exc: Exception) -> bool:
    """True if a streaming failure looks like a transient upstream/network issue
    that a non-streaming retry can likely recover from. Authentication errors,
    model-not-found, and other deterministic 4xx are NOT recoverable."""
    msg = str(exc).lower()
    keywords = (
        "temporarily unavailable", "upstream", "connection reset",
        "remote end closed", "timeout", "timed out",
        "502", "503", "504", "bad gateway", "incomplete chunked",
        "connection aborted", "connection broken",
    )
    return any(k in msg for k in keywords)


def _stream_openai(client: OpenAI, kwargs: dict, on_delta: callable) -> MessageResponse:
    """Stream chunks from OpenAI, calling on_delta(text, event_type) for TUI updates."""
    from harness.ui.events import is_enabled

    stream = client.chat.completions.create(**kwargs)
    text_parts: list[str] = []
    tool_call_buf: dict[str, dict] = {}  # index -> {id, name, args}
    finish_reason: str | None = None
    model: str | None = None
    usage = None

    for chunk in stream:
        if not chunk.choices:
            usage = usage or getattr(chunk, 'usage', None)
            model = model or getattr(chunk, 'model', None)
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        finish_reason = finish_reason or getattr(choice, 'finish_reason', None)
        model = model or getattr(chunk, 'model', None)
        usage = usage or getattr(chunk, 'usage', None)

        if delta is None:
            continue

        # Text delta
        if delta.content:
            text_parts.append(delta.content)
            if is_enabled():
                on_delta(delta.content, "text_delta")

        # Tool call delta
        for tc in delta.tool_calls or []:
            idx = tc.index
            buf = tool_call_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.id:
                buf["id"] = tc.id
            if tc.function and tc.function.name:
                buf["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                buf["args"] += tc.function.arguments

    # Build blocks from accumulated stream
    blocks: list = []
    if text_parts:
        blocks.append(TextBlock(text="".join(text_parts)))

    has_tool_calls = bool(tool_call_buf)
    for buf in sorted(tool_call_buf.values(), key=lambda b: list(tool_call_buf.keys())[list(tool_call_buf.values()).index(b)]):
        try:
            parsed = json.loads(buf["args"] or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": buf["args"]}
        blocks.append(ToolUseBlock(
            id=buf["id"] or f"call_{uuid.uuid4().hex[:12]}",
            name=buf["name"],
            input=parsed if isinstance(parsed, dict) else {"value": parsed},
        ))

    return MessageResponse(
        content=blocks,
        stop_reason=_map_stop_reason(finish_reason, has_tool_calls),
        model=model,
        usage=usage,
    )
