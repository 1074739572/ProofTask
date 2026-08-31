"""Deterministic Goal-envelope budget accounting.

Worker slices remain bounded by their existing per-call limits.  This module
adds cumulative accounting across a Goal lifetime without making the model a
source of truth for stopping decisions.
"""

from __future__ import annotations

import time
from typing import Any

LEDGER_KEYS = (
    "active_seconds",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_requests",
    "tool_calls",
    "verification_runs",
    "worker_slices",
    "repair_attempts",
    "replan_attempts",
)


def ensure_ledger(state: Any) -> dict[str, float]:
    ledger = state.usage_ledger if isinstance(getattr(state, "usage_ledger", None), dict) else {}
    normalized = {key: max(0.0, float(ledger.get(key, 0) or 0)) for key in LEDGER_KEYS}
    state.usage_ledger = normalized
    return normalized


def add_usage(state: Any, **values: float | int) -> dict[str, float]:
    ledger = ensure_ledger(state)
    for key, value in values.items():
        if key not in LEDGER_KEYS:
            continue
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            continue
        ledger[key] = max(0.0, ledger.get(key, 0.0) + amount)
    return ledger


def active_seconds(state: Any, now: float | None = None) -> float:
    """Return active wall time, excluding durable paused intervals."""
    now = time.time() if now is None else now
    started = float(getattr(state, "started_at", now) or now)
    paused_at = getattr(state, "paused_at", None)
    end = now if getattr(state, "status", "") not in {"paused", "done", "failed", "cancelled"} else float(paused_at or now)
    return max(0.0, end - started)


def refresh_active_seconds(state: Any, now: float | None = None) -> float:
    seconds = active_seconds(state, now)
    ensure_ledger(state)["active_seconds"] = seconds
    return seconds


def exhausted(state: Any, now: float | None = None) -> str | None:
    ledger = ensure_ledger(state)
    refresh_active_seconds(state, now)
    limits = getattr(state, "budget_limits", None)
    if not isinstance(limits, dict):
        return None
    for key in LEDGER_KEYS:
        raw_limit = limits.get(key)
        if raw_limit in (None, "", 0):
            continue
        try:
            limit = float(raw_limit)
        except (TypeError, ValueError):
            continue
        if limit > 0 and ledger.get(key, 0.0) >= limit:
            return key
    return None


def remaining(state: Any) -> dict[str, float | None]:
    ledger = ensure_ledger(state)
    limits = getattr(state, "budget_limits", {})
    return {
        key: (max(0.0, float(limits[key]) - ledger[key]) if isinstance(limits, dict) and key in limits else None)
        for key in LEDGER_KEYS
    }
