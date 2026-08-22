"""Normalize provider usage / cache token fields across APIs."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class CacheUsage:
    hit_tokens: int
    miss_tokens: int
    output_tokens: int | None
    source: str

    @property
    def input_tokens(self) -> int:
        return self.hit_tokens + self.miss_tokens

    @property
    def hit_rate(self) -> float:
        total = self.input_tokens
        return self.hit_tokens / total if total else 0.0


def parse_cache_usage(usage) -> CacheUsage | None:
    """Extract cache hit/miss from Anthropic, DeepSeek, or OpenAI-compatible usage objects."""
    if usage is None:
        return None

    # Anthropic names this ``output_tokens``. OpenAI-compatible providers
    # (including DeepSeek's cache-aware responses) use ``completion_tokens``.
    # Read both before taking a cache-field branch so cached requests do not
    # silently become "0 output" in the local usage ledger.
    def field(name: str, default=None):
        if isinstance(usage, Mapping):
            return usage.get(name, default)
        return getattr(usage, name, default)

    output = field("output_tokens")
    if output is None:
        output = field("completion_tokens")
    if output is not None:
        output = int(output)

    # DeepSeek / Anthropic prompt caching (Messages API)
    read = field("cache_read_input_tokens")
    create = field("cache_creation_input_tokens")
    input_tokens = field("input_tokens")
    if read is not None or create is not None:
        hit = int(read or 0)
        billed_input = int(input_tokens or 0)
        miss = billed_input
        if hit == 0 and create:
            miss = max(miss, int(create))
        return CacheUsage(
            hit_tokens=hit,
            miss_tokens=miss,
            output_tokens=output,
            source="cache_read_input_tokens",
        )

    hit = field("prompt_cache_hit_tokens")
    miss = field("prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        return CacheUsage(
            hit_tokens=int(hit or 0),
            miss_tokens=int(miss or 0),
            output_tokens=output,
            source="prompt_cache_hit_tokens",
        )

    # OpenAI-style usage: prompt_tokens + completion_tokens
    prompt = field("prompt_tokens")
    completion = field("completion_tokens")
    if prompt is not None:
        # Check for cached tokens in prompt_tokens_details
        details = field("prompt_tokens_details")
        cached_value = details.get("cached_tokens", 0) if isinstance(details, Mapping) else getattr(details, "cached_tokens", 0)
        cached = int(cached_value or 0) if details else 0
        miss = int(prompt) - cached if cached else int(prompt)
        return CacheUsage(
            hit_tokens=cached,
            miss_tokens=max(0, miss),
            output_tokens=int(completion) if completion is not None else output,
            source="openai_prompt_tokens",
        )

    if input_tokens is not None:
        return CacheUsage(
            hit_tokens=0,
            miss_tokens=int(input_tokens),
            output_tokens=output,
            source="input_tokens_only",
        )
    return None
