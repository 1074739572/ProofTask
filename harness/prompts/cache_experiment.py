"""Simulate and measure prompt cache hit/miss across assembly strategies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Literal

from harness.prompts import assemble_system_prompt
from harness.prompts.dynamic import build_session_context
from harness.prompts.static import assemble_static_system_prompt

StrategyName = Literal[
    "legacy_system",
    "current",
    "ephemeral_if_unchanged",
    "time_minute",
    "time_none",
    "slim_context",
]

CHARS_PER_TOKEN = 4


@dataclass
class RoundMetrics:
    round_index: int
    hit_tokens: int
    miss_tokens: int
    input_tokens: int
    ephemeral_tokens: int
    history_tokens: int
    system_tokens: int

    @property
    def hit_rate(self) -> float:
        total = self.hit_tokens + self.miss_tokens
        return self.hit_tokens / total if total else 0.0


@dataclass
class StrategyResult:
    name: str
    rounds: list[RoundMetrics] = field(default_factory=list)

    @property
    def total_hit(self) -> int:
        return sum(r.hit_tokens for r in self.rounds)

    @property
    def total_miss(self) -> int:
        return sum(r.miss_tokens for r in self.rounds)

    @property
    def hit_rate(self) -> float:
        total = self.total_hit + self.total_miss
        return self.total_hit / total if total else 0.0

    @property
    def avg_miss_per_round(self) -> float:
        return self.total_miss / len(self.rounds) if self.rounds else 0.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // CHARS_PER_TOKEN)


def _common_byte_prefix_len(a: str, b: str) -> int:
    ab, bb = a.encode("utf-8"), b.encode("utf-8")
    limit = min(len(ab), len(bb))
    for index in range(limit):
        if ab[index] != bb[index]:
            return index
    return limit


def split_hit_miss(previous: str | None, current: str) -> tuple[int, int]:
    """Byte-prefix cache model: hit = shared prefix with prior request, miss = rest."""
    if not previous:
        miss = estimate_tokens(current)
        return 0, miss
    shared_bytes = _common_byte_prefix_len(previous, current)
    total_bytes = len(current.encode("utf-8"))
    if total_bytes == 0:
        return 0, 0
    hit_bytes = min(shared_bytes, total_bytes)
    hit = int(hit_bytes / CHARS_PER_TOKEN)
    input_tokens = estimate_tokens(current)
    miss = max(0, input_tokens - hit)
    return hit, miss


def _session_context_options(strategy: StrategyName) -> dict:
    if strategy == "time_minute":
        return {"time_granularity": "minute"}
    if strategy == "time_none":
        return {"include_time": False}
    if strategy == "slim_context":
        return {
            "include_time": False,
            "include_mode": False,
            "include_memories": False,
            "include_mcp": False,
            "include_teammates": False,
        }
    return {}


def _build_legacy_system(
    context: dict,
    strategy: StrategyName,
    *,
    runtime_tick: int | None = None,
) -> str:
    static = assemble_static_system_prompt()
    opts = _session_context_options(strategy)
    dynamic = build_session_context(context, **opts)
    if runtime_tick is not None and opts.get("include_time", True):
        dynamic += f"\n\nCurrent time (simulated): 2026-01-01T10:{runtime_tick:02d}:00"
    return f"{static}\n\n{dynamic}" if dynamic.strip() else static


def build_request_payload(
    *,
    strategy: StrategyName,
    messages: list[dict],
    context: dict,
    tools_blob: str,
    last_ephemeral_body: str | None = None,
    runtime_tick: int | None = None,
) -> tuple[str, list[dict], str | None]:
    """Return (system, api_messages, ephemeral_body_used)."""
    if strategy == "legacy_system":
        return (
            _build_legacy_system(context, strategy, runtime_tick=runtime_tick),
            list(messages),
            None,
        )

    # ``current`` mirrors the live generic agent: all request-level input is
    # durable history, while only session/task-scoped guidance appears in the
    # system prompt. The legacy strategy above is intentionally the old bug.
    _ = last_ephemeral_body, runtime_tick
    return assemble_system_prompt(context), list(messages), None


def canonical_request_string(system: str, tools_blob: str, messages: list[dict]) -> str:
    """Single string representing full API input (tools → system → messages order)."""
    parts = [
        "[[tools]]\n",
        tools_blob,
        "\n[[system]]\n",
        system,
        "\n[[messages]]\n",
        json.dumps(messages, ensure_ascii=False, sort_keys=True),
    ]
    return "".join(parts)


def sample_tool_blob(size_tokens: int = 800) -> str:
    """Stub tool JSON sized like a real registry payload."""
    chunk = (
        '{"name":"bash","description":"Run shell","input_schema":{"type":"object"}}'
    )
    repeats = max(1, (size_tokens * CHARS_PER_TOKEN) // len(chunk))
    return "[" + ",".join([chunk] * repeats) + "]"


def sample_context(*, with_todos: bool = True) -> dict:
    ctx: dict = {
        "memories": "Project uses Python 3.11.",
        "connected_mcp": ["docs"],
        "active_teammates": [],
    }
    return ctx


def sample_todos() -> list[dict[str, str]]:
    return [
        {
            "content": "Read harness loop",
            "activeForm": "Reading harness loop…",
            "status": "completed",
        },
        {
            "content": "Run cache experiment",
            "activeForm": "Running cache experiment…",
            "status": "in_progress",
        },
        {
            "content": "Summarize results",
            "activeForm": "Summarizing results…",
            "status": "pending",
        },
    ]


def simulate_tool_loop(
    strategy: StrategyName,
    *,
    rounds: int = 8,
    tool_blob_tokens: int = 800,
    tool_result_chars: int = 1200,
) -> StrategyResult:
    """Simulate agent tool-loop LLM calls and estimate cache hit/miss per round."""
    from harness.todos.state import clear_todos, set_todos

    clear_todos()
    set_todos(sample_todos())

    context = sample_context()
    tools_blob = sample_tool_blob(tool_blob_tokens)
    messages: list[dict] = [
        {"role": "user", "content": "Analyze the cache experiment module and suggest improvements."}
    ]

    result = StrategyResult(name=strategy)
    previous: str | None = None
    last_ephemeral_body: str | None = None

    for index in range(rounds):
        system, api_messages, ephemeral_body = build_request_payload(
            strategy=strategy,
            messages=messages,
            context=context,
            tools_blob=tools_blob,
            last_ephemeral_body=last_ephemeral_body,
            # A tool loop can cross minute boundaries. This synthetic clock
            # catches exactly the regression where dynamic system assembly
            # invalidates the request prefix after the cache's first block.
            runtime_tick=index,
        )
        if ephemeral_body is not None:
            last_ephemeral_body = ephemeral_body

        current = canonical_request_string(system, tools_blob, api_messages)
        hit, miss = split_hit_miss(previous, current)

        result.rounds.append(
            RoundMetrics(
                round_index=index + 1,
                hit_tokens=hit,
                miss_tokens=miss,
                input_tokens=hit + miss,
                ephemeral_tokens=0,
                history_tokens=estimate_tokens(
                    json.dumps(messages, ensure_ascii=False)
                ),
                system_tokens=estimate_tokens(system),
            )
        )
        previous = current

        if index >= rounds - 1:
            break

        # Simulate assistant tool_use + user tool_result (growing history).
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tu_{index}",
                        "name": "read_file",
                        "input": {"path": f"harness/file_{index}.py"},
                    }
                ],
            }
        )
        filler = "x" * tool_result_chars
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"tu_{index}",
                        "content": f"File contents chunk {index}:\n{filler}",
                    }
                ],
            }
        )

        if index == 2:
            # Todo update mid-loop — changes ephemeral body for subsequent strategies.
            set_todos(
                [
                    sample_todos()[0],
                    {**sample_todos()[1], "status": "completed"},
                    {**sample_todos()[2], "status": "in_progress", "activeForm": "Summarizing…"},
                ]
            )

    clear_todos()
    return result


def run_all_simulations(
    *,
    rounds: int = 8,
    strategies: list[StrategyName] | None = None,
) -> list[StrategyResult]:
    names: list[StrategyName] = strategies or [
        "legacy_system",
        "current",
    ]
    return [simulate_tool_loop(name, rounds=rounds) for name in names]


def format_results_table(results: list[StrategyResult]) -> str:
    headers = ("strategy", "hit_rate", "total_hit", "total_miss", "avg_miss/round")
    rows = [
        (
            r.name,
            f"{100 * r.hit_rate:.1f}%",
            str(r.total_hit),
            str(r.total_miss),
            f"{r.avg_miss_per_round:.0f}",
        )
        for r in sorted(results, key=lambda item: item.hit_rate, reverse=True)
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    lines.extend(
        "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in rows
    )
    return "\n".join(lines)


def context_body_hash(context: dict, **options) -> str:
    body = build_session_context(context, **options).strip()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
