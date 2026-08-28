"""Central LLM calls with provider routing and API visibility."""

from __future__ import annotations

from harness.models import get_model_profile
from harness.providers.router import create_provider_message
from harness.ui.renderer import renderer
from harness.usage import parse_cache_usage, record_usage


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
        emit(
            "usage_update",
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            output_tokens_known=parsed.output_tokens is not None,
            cache_read_tokens=parsed.hit_tokens,
            context_tokens=parsed.input_tokens if is_primary_turn and session_id else None,
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

    with renderer.llm_busy(_format_llm_tag(profile)):
        response = create_provider_message(
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
