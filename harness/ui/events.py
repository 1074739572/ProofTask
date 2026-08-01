"""Structured UI event emission for alternate frontends.

Classic CLI keeps rendering through :mod:`harness.ui.renderer`.  When enabled,
this module mirrors the same high-level events as JSONL so a separate TUI
process can render them without scraping human text output.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, TextIO

_enabled = False
_sink: TextIO | None = None
_lock = threading.Lock()
_seq = 0
_suppress_depth = 0


def is_enabled() -> bool:
    return _enabled and _sink is not None


def enable_event_stream(sink: TextIO | None = None) -> None:
    """Enable JSONL event output to *sink* (defaults to stdout)."""
    global _enabled, _sink
    _sink = sink or sys.stdout
    _enabled = True


def disable_event_stream() -> None:
    global _enabled, _sink
    _enabled = False
    _sink = None


@contextmanager
def suppress_events():
    """Temporarily suppress mirrored events around internal tool calls."""
    global _suppress_depth
    _suppress_depth += 1
    try:
        yield
    finally:
        _suppress_depth = max(0, _suppress_depth - 1)


def _json_default(value: Any) -> str:
    return str(value)


def emit(event_type: str, **payload: Any) -> None:
    """Emit a single JSONL UI event if the stream is enabled."""
    global _seq
    if not is_enabled() or _suppress_depth:
        return
    assert _sink is not None
    with _lock:
        _seq += 1
        event = {
            "seq": _seq,
            "ts": time.time(),
            "type": event_type,
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False, default=_json_default)
        try:
            _sink.write(line + "\n")
            _sink.flush()
        except Exception:
            # UI mirroring must never break the agent loop.
            if os.getenv("HARNESS_EVENT_DEBUG", "0") == "1":
                raise


def short_text(text: Any, limit: int = 600) -> str:
    value = str(text if text is not None else "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"
