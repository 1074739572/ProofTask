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
    parts = [str(exc)]
    cause = exc
    while getattr(cause, "__cause__", None) is not None:
        cause = cause.__cause__
        parts.append(str(cause))
    msg = " ".join(parts).lower()
    # Windows socket permission failures are deterministic. Retrying them
    # only obscures the real problem and makes every configured model appear
    # unavailable.
    if "10013" in msg or "access is denied" in msg or "permission denied" in msg:
        return False
    return any(
        token in name or token in msg
        for token in ("timeout", "connection", "temporarily unavailable", "502", "503", "529")
    )


def with_retry(fn, state: RecoveryState, *, max_attempts: int = MAX_RETRIES):
    """Run a request with a bounded number of attempts."""
    attempts = max(1, int(max_attempts))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as exc:
            last_error = exc
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
    detail = f"{type(last_error).__name__}: {last_error}" if last_error is not None else "unknown error"
    raise RuntimeError(f"Max retries ({attempts}) exceeded; last error: {detail}") from last_error


def is_model_recoverable_error(exc: Exception) -> bool:
    """Whether switching to a different model/provider is worth trying."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None) or getattr(exc, "code", None)
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    return bool(
        status in (403, 404, 429, 500, 502, 503, 529)
        or any(
            token in name or token in msg
            for token in (
                "insufficient_balance",
                "insufficient balance",
                "billing",
                "quota",
                "permission",
                "forbidden",
                "does not exist",
                "not found",
                "overloaded",
                "timeout",
                "connection",
                "temporarily unavailable",
            )
        )
    )


def candidate_recovery_models(current_model: str | None = None) -> list[str]:
    """Return configured fallback candidates with API keys present.

    Priority:
    1. HARNESS_RECOVERY_MODELS comma list
    2. FALLBACK_MODEL_ID
    3. Other configured models whose providers have keys, preferring a different provider
    """
    import os

    from harness.models import get_model, get_model_profile, list_models
    from harness.providers.config import get_provider, resolve_api_key

    current = current_model or get_model()
    explicit = [m.strip() for m in os.getenv("HARNESS_RECOVERY_MODELS", "").split(",") if m.strip()]
    if FALLBACK_MODEL:
        explicit.append(FALLBACK_MODEL)

    catalog = list_models()
    known = {m["id"]: m for m in catalog}

    def has_key(model_id: str) -> bool:
        entry = known.get(model_id)
        if not entry:
            return False
        try:
            return bool(resolve_api_key(get_provider(entry.get("provider", "deepseek"))))
        except Exception:
            return False

    ordered: list[str] = []
    for model_id in explicit:
        if model_id != current and has_key(model_id) and model_id not in ordered:
            ordered.append(model_id)

    try:
        current_provider = get_model_profile(current).provider
    except Exception:
        current_provider = ""

    other_provider = []
    same_provider = []
    for entry in catalog:
        model_id = entry["id"]
        if model_id == current or model_id in ordered:
            continue
        if not has_key(model_id):
            continue
        if entry.get("provider") != current_provider:
            other_provider.append(model_id)
        else:
            same_provider.append(model_id)
    return [*ordered, *other_provider, *same_provider]


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
