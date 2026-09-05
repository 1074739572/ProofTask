"""Central LLM calls with provider routing and API visibility."""

from __future__ import annotations

import threading

from harness.agent.cancel import is_cancelled
from harness.models import get_model_profile
from harness.providers.router import create_provider_message
from harness.ui.renderer import renderer
from harness.usage import parse_cache_usage, record_usage


class _CancelledLLMRequest(RuntimeError):
    """Internal signal used to stop waiting for a provider request."""


def _call_provider_cancellable(*, profile, messages, max_tokens, system, tools,
                               on_delta, read_timeout_seconds):
    """Run a provider request without making Esc wait for the HTTP timeout.

    Provider SDK calls are synchronous and can block while connecting or
    waiting for the first response chunk.  The agent loop cannot observe its
    cooperative cancel flag during that call, so run it in a daemon worker and
    poll the flag here.  The worker is deliberately isolated from the turn
    after cancellation; its stream callback also checks the per-request event
    so late chunks cannot leak into a subsequent turn.
    """
    done = threading.Event()
    abandoned = threading.Event()
    result: dict[str, object] = {}

    if is_cancelled():
        raise _CancelledLLMRequest("LLM request cancelled")

    def _delta(text: str, event_type: str) -> None:
        if abandoned.is_set() or is_cancelled():
            raise _CancelledLLMRequest("LLM request cancelled")
        if on_delta is not None:
            on_delta(text, event_type)

    def _run() -> None:
        try:
            result["response"] = create_provider_message(
                profile=profile,
                messages=messages,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                on_delta=_delta if on_delta is not None else None,
                read_timeout_seconds=read_timeout_seconds,
            )
        except BaseException as exc:  # pass provider failures to the caller
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_run, name="harness-llm-request", daemon=True).start()
    while not done.wait(0.05):
        if is_cancelled():
            abandoned.set()
            raise _CancelledLLMRequest("LLM request cancelled")
    # A cancel can race with the final response, and must still win for this
    # turn.  This also prevents usage accounting for a response the user
    # explicitly discarded.
    if is_cancelled():
        abandoned.set()
        raise _CancelledLLMRequest("LLM request cancelled")
    error = result.get("error")
    if error is not None:
        raise error
    return result["response"]


def _format_llm_tag(profile) -> str:
    tag = f"{profile.provider}/{profile.api_model}"
    if profile.thinking:
        tag += "+thinking"
    if profile.api_model != profile.id:
        tag = f"{profile.id}→{tag}"
    return tag


def _log_cache_usage(response, *, model_id: str, usage_context: dict[str, str] | None = None) -> None:
    """Persist usage; do not spam the terminal on every LLM round."""
    from harness.ui.tool_display import hooks_verbose

    parsed = parse_cache_usage(getattr(response, "usage", None))
    if parsed is None:
        return
    metadata = dict(usage_context or {})
    session_id = str(metadata.get("session_id") or "")
    is_primary_turn = not metadata.get("agent_type")
    if is_primary_turn and session_id:
        from harness.usage.context import record_prompt_tokens

        record_prompt_tokens(session_id, parsed.input_tokens)
    try:
        record_usage(model=model_id, cache=parsed, context=metadata)
    except OSError as exc:
        renderer.warn(f"usage ledger write failed: {exc}")
    from harness.ui.events import emit, is_enabled
    if is_enabled():
        context_breakdown: dict[str, int] | None = None
        if is_primary_turn and session_id:
            from harness.usage.context import scaled_context_breakdown

            context_breakdown = scaled_context_breakdown(session_id, parsed.input_tokens)
        emit(
            "usage_update",
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            output_tokens_known=parsed.output_tokens is not None,
            cache_read_tokens=parsed.hit_tokens,
            context_tokens=parsed.input_tokens if is_primary_turn and session_id else None,
            context_system=context_breakdown["system"] if context_breakdown else None,
            context_tools=context_breakdown["tools"] if context_breakdown else None,
            context_messages=context_breakdown["messages"] if context_breakdown else None,
            **metadata,
        )
    if not hooks_verbose():
        return
    rate = f"{100 * parsed.hit_rate:.0f}%"
    out_part = f" out={parsed.output_tokens}" if parsed.output_tokens is not None else ""
    renderer.muted(
        f"  [cache] hit={parsed.hit_tokens} miss={parsed.miss_tokens} ({rate}){out_part}"
    )


def create_message(
    *,
    messages: list,
    max_tokens: int,
    system: str | None = None,
    tools: list | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
    inherit_interactive_effort: bool = True,
    usage_context: dict[str, str] | None = None,
    force_stream: bool = False,
    read_timeout_seconds: float | None = None,
):
    from harness.ui.events import emit as _emit, is_enabled as _is_enabled

    profile = get_model_profile(
        model_id,
        effort_override=reasoning_effort,
        inherit_interactive_effort=inherit_interactive_effort,
    )

    def _delta_callback(text: str, _event_type: str) -> None:
        _emit("assistant_delta", text=text, model=profile.api_model)

    # Headless structured jobs still benefit from transport streaming: relays
    # can keep a long reasoning response alive by sending chunks, while a
    # non-streaming request must finish its whole response before the read
    # timeout expires. Keep that traffic out of the user-facing event stream.
    on_delta = _delta_callback if _is_enabled() else (lambda _text, _kind: None) if force_stream else None

    # Measure the request composition for the context breakdown meter. Only
    # primary turns shape the session context the footer reports on; subagent
    # calls run in their own scratch context.
    _meta = dict(usage_context or {})
    _session_id = str(_meta.get("session_id") or "")
    if _session_id and not _meta.get("agent_type"):
        import json as _json

        from harness.agent.compact.sizing import estimate_tokens as _estimate_tokens
        from harness.usage.context import record_context_breakdown

        record_context_breakdown(
            _session_id,
            system_tokens=len(system or "") // 4,
            tools_tokens=len(_json.dumps(tools, default=str)) // 4 if tools else 0,
            messages_tokens=_estimate_tokens(messages or []),
        )

    with renderer.llm_busy(_format_llm_tag(profile)):
        response = _call_provider_cancellable(
            profile=profile,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            on_delta=on_delta,
            read_timeout_seconds=read_timeout_seconds,
        )

    _log_cache_usage(response, model_id=profile.id, usage_context=usage_context)

    reported = getattr(response, "model", None)
    if reported and reported != profile.api_model:
        renderer.warn(
            f"API backend used '{reported}' (requested '{profile.api_model}')"
        )
    return response
