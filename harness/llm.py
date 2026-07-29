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


def _log_cache_usage(response, *, model_id: str) -> None:
    """Persist usage; do not spam the terminal on every LLM round."""
    from harness.ui.tool_display import hooks_verbose

    parsed = parse_cache_usage(getattr(response, "usage", None))
    if parsed is None:
        return
    try:
        record_usage(model=model_id, cache=parsed)
    except OSError as exc:
        renderer.warn(f"usage ledger write failed: {exc}")
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
):
    profile = get_model_profile(model_id)

    with renderer.llm_busy(_format_llm_tag(profile)):
        response = create_provider_message(
            profile=profile,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
        )

    _log_cache_usage(response, model_id=profile.id)

    reported = getattr(response, "model", None)
    if reported and reported != profile.api_model:
        renderer.warn(
            f"API backend used '{reported}' (requested '{profile.api_model}')"
        )
    return response
