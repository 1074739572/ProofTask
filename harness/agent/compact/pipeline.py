"""Compaction pipeline: prepare_context, auto compact, reactive compact."""

from __future__ import annotations

import json
import time
from pathlib import Path

from harness.agent.compact.layers import keep_tail, snip_compact
from harness.agent.compact.messages import ensure_latest_user_focus
from harness.agent.compact.sizing import (
    estimate_size,
    estimate_tokens,
    should_autocompact,
)
from harness.agent.compact.summarize import is_summary_unusable, summarize_history
from harness.settings import TRANSCRIPT_DIR, compact_tail_count


def write_transcript(messages: list) -> Path:
    from harness.project.session import serialize_messages

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in serialize_messages(messages):
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")
    return path


def _build_compacted(label: str, summary: str, messages: list) -> list:
    tail = keep_tail(messages, count=compact_tail_count())
    compacted = [
        {"role": "user", "content": f"[{label}]\n\n{summary}"},
        *tail,
    ]
    return ensure_latest_user_focus(compacted, messages)


def _degraded_compact(label: str, messages: list) -> list:
    """When LLM summary is empty/unusable: keep recent turns, never empty summary.

    Prefer a lossy snip + tail over ``(empty summary)`` amnesia (GAIA Pie Menus /
    deepseek thinking-only replies).
    """
    keep_n = max(20, compact_tail_count() * 4)
    reduced = snip_compact(list(messages), max_messages=keep_n)
    notice = (
        f"[{label}]\n\n"
        "Structured summary was empty or unavailable (common with reasoning "
        "models that put text only in thinking blocks). Recent messages were "
        "kept instead — do NOT assume prior facts were verified; re-read the "
        "retained tool results / user question before answering or searching again."
    )
    return ensure_latest_user_focus(
        [{"role": "user", "content": notice}, *keep_tail(reduced, count=compact_tail_count())],
        messages,
    )


def compact_history(messages: list, *, binding=None) -> list:
    from harness.project.session_store import record_compact_boundary
    from harness.ui.tool_display import hooks_verbose

    transcript = write_transcript(messages)
    if hooks_verbose():
        print(f"  \033[36m[compact] transcript saved: {transcript}\033[0m")
    summary = summarize_history(messages)
    if is_summary_unusable(summary):
        if hooks_verbose():
            print(
                "  \033[33m[compact] summary unusable — keeping recent "
                "messages (no empty summary)\033[0m"
            )
        compacted = _degraded_compact("Compacted — summary unavailable", messages)
    else:
        compacted = _build_compacted("Compacted", summary, messages)
    if binding is not None:
        record_compact_boundary("auto", estimate_size(messages), transcript, compacted, binding=binding)
    return compacted


def reactive_compact(messages: list, *, binding=None) -> list:
    from harness.project.session_store import record_compact_boundary
    from harness.ui.tool_display import hooks_verbose

    transcript = write_transcript(messages)
    if hooks_verbose():
        print(f"  \033[31m[reactive compact] transcript saved: {transcript}\033[0m")
    summary = summarize_history(messages)
    if is_summary_unusable(summary):
        if hooks_verbose():
            print(
                "  \033[33m[reactive compact] summary unusable — keeping "
                "recent messages\033[0m"
            )
        compacted = _degraded_compact("Reactive compact — summary unavailable", messages)
    else:
        compacted = _build_compacted("Reactive compact", summary, messages)
    if binding is not None:
        record_compact_boundary("reactive", estimate_size(messages), transcript, compacted, binding=binding)
    return compacted


def prepare_context(messages: list) -> list:
    """
    Keep sent history append-only until a real compaction checkpoint.

    Tool outputs are bounded before they enter history. Progressively rewriting
    old messages here would invalidate the exact prefix reused by provider
    prompt caches on every tool round.
    """
    # Claude Code–style checkpoint: estimate_tokens ≳ 0.835 × context_window.
    if should_autocompact(messages):
        messages[:] = compact_history(messages)
    return messages
