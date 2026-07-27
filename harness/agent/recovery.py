"""LLM call retry and recovery strategies."""

from __future__ import annotations

import random
import time

from harness.agent.cancel import is_cancelled
from harness.settings import (
    BASE_DELAY_MS,
    FALLBACK_MODEL,
    MAX_RETRIES,
    MAX_CONSECUTIVE_529,
)


class RecoveryState:
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.fallback_model: str | None = None
        self.has_nudged_empty_reply = False
        self.has_nudged_web_budget = False
        # After LookupGuard consecutive blocks: strip tools until a text answer.
        self.strip_tools_until_answer = False
        self.has_lookup_force_finalize = False


def retry_delay(attempt: int) -> float:
    base = min(BASE_DELAY_MS * (2**attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def _sleep_cancelable(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_cancelled():
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return True


def _report_retry(kind: str, attempt: int, delay: float) -> None:
    from harness.ui.renderer import renderer

    message = f"{kind} retry {attempt + 1}/{MAX_RETRIES} after {delay:.1f}s"
    renderer.warn(message)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return any(
        token in name or token in msg
        for token in ("timeout", "connection", "temporarily unavailable", "502", "503", "529")
    )


def with_retry(fn, state: RecoveryState):
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as exc:
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                _report_retry("429", attempt, delay)
                if not _sleep_cancelable(delay):
                    raise RuntimeError("cancelled during retry")
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    state.fallback_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                _report_retry("529", attempt, delay)
                if not _sleep_cancelable(delay):
                    raise RuntimeError("cancelled during retry")
                continue
            if _is_transient(exc):
                delay = retry_delay(attempt)
                _report_retry("network", attempt, delay)
                if not _sleep_cancelable(delay):
                    raise RuntimeError("cancelled during retry")
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        ("prompt" in msg and "long" in msg)
        or "context_length_exceeded" in msg
        or "max_context_window" in msg
        or "range of input length" in msg
        or "input length should be" in msg
        or "maximum context length" in msg
        or "input_too_long" in msg
    )
