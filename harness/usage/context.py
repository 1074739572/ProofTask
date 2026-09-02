"""Authoritative current-context token values reported by providers."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_latest_prompt_tokens: dict[str, int] = {}
_latest_breakdown: dict[str, dict[str, int]] = {}


def record_prompt_tokens(session_id: str, prompt_tokens: int) -> None:
    """Remember the latest provider-reported prompt size for one session."""
    if not session_id:
        return
    with _lock:
        _latest_prompt_tokens[session_id] = max(0, int(prompt_tokens))


def current_context_tokens(session_id: str, estimated_tokens: int) -> int:
    """Prefer an API value, falling back to the local pre-request estimate."""
    with _lock:
        actual = _latest_prompt_tokens.get(session_id, 0)
    return actual or max(0, int(estimated_tokens))


def record_context_breakdown(
    session_id: str,
    *,
    system_tokens: int,
    tools_tokens: int,
    messages_tokens: int,
) -> None:
    """Remember the request-build composition for one session.

    Measured when the request is assembled (chars/4, the same heuristic the
    rest of the harness uses). Ratios come from this measurement; absolute
    values are scaled to the provider-reported total on read.
    """
    if not session_id:
        return
    with _lock:
        _latest_breakdown[session_id] = {
            "system": max(0, int(system_tokens)),
            "tools": max(0, int(tools_tokens)),
            "messages": max(0, int(messages_tokens)),
        }


def scaled_context_breakdown(session_id: str, total_tokens: int) -> dict[str, int]:
    """Category token counts scaled so they sum to ``total_tokens``.

    The provider-reported prompt total is the ground truth; the recorded
    request-build measurement supplies the ratios. Without a measurement
    (before the first LLM call) everything is attributed to messages, which
    is exactly what the history-only estimate contains.
    """
    total = max(0, int(total_tokens))
    with _lock:
        raw = _latest_breakdown.get(session_id)
    if not raw:
        return {"system": 0, "tools": 0, "messages": total}
    raw_sum = raw["system"] + raw["tools"] + raw["messages"]
    if raw_sum <= 0 or total <= 0:
        return {"system": 0, "tools": 0, "messages": total}
    system = round(raw["system"] * total / raw_sum)
    tools = round(raw["tools"] * total / raw_sum)
    # Messages absorb the rounding remainder so parts always sum to total.
    return {"system": system, "tools": tools, "messages": max(0, total - system - tools)}
