"""Authoritative current-context token values reported by providers."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_latest_prompt_tokens: dict[str, int] = {}


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
