"""Compaction pipeline: prepare_context, auto compact, reactive compact."""

from __future__ import annotations

import json
import time
from pathlib import Path

from harness.agent.compact.layers import keep_tail, snip_compact
from harness.agent.compact.messages import ensure_latest_user_focus, find_latest_user_text
from harness.agent.compact.sizing import (
    estimate_size,
    estimate_tokens,
    should_autocompact,
)
from harness.agent.compact.summarize import is_summary_unusable, summarize_history
from harness.settings import TRANSCRIPT_DIR, compact_tail_count


def collect_resume_state(messages: list, transcript: Path) -> str:
    """Deterministic resume state for compacted history.

    The summary model must not have to guess these fields; they come from
    runtime state (latest user request, session todos, active Goal/Task and
    its bound verification) plus the transcript path. Each source is
    best-effort — an unavailable store never blocks compaction.
    """
    lines = [
        "## Resume State (deterministic — trust over the summary above)",
    ]
    latest = find_latest_user_text(messages)
    if latest:
        lines.append(f"- Latest user request: {latest}")
    try:
        from harness.todos.state import get_todos

        pending = [
            todo for todo in get_todos() if todo.get("status") != "completed"
        ]
        if pending:
            lines.append("- Session todos (unfinished):")
            for todo in pending[:10]:
                lines.append(f"  - [{todo.get('status', '?')}] {todo.get('content', '')}")
    except Exception:
        pass
    try:
        from harness.goal.store import load_goal

        state = load_goal()
        if state is not None:
            lines.append(
                f"- Active Goal: {state.id} — {state.target} "
                f"(status={state.status}, phase={state.phase})"
            )
            if state.current_task_id:
                lines.append(f"- Current Task: {state.current_task_id}")
            if state.verification:
                lines.append(f"- Goal verification: {state.verification}")
    except Exception:
        pass
    lines.append(f"- Full pre-compact transcript: {transcript}")
    return "\n".join(lines)


def write_transcript(messages: list) -> Path:
    from harness.project.session import serialize_messages

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in serialize_messages(messages):
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")
    return path


def _build_compacted(label: str, summary: str, messages: list, transcript: Path) -> list:
    tail = keep_tail(messages, count=compact_tail_count())
    compacted = [
        {"role": "user", "content": f"[{label}]\n\n{summary}\n\n{collect_resume_state(messages, transcript)}"},
        *tail,
    ]
    return ensure_latest_user_focus(compacted, messages)


def _degraded_compact(label: str, messages: list, transcript: Path) -> list:
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
    head = f"{notice}\n\n{collect_resume_state(messages, transcript)}"
    return ensure_latest_user_focus(
        [{"role": "user", "content": head}, *keep_tail(reduced, count=compact_tail_count())],
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
        compacted = _degraded_compact("Compacted — summary unavailable", messages, transcript)
    else:
        compacted = _build_compacted("Compacted", summary, messages, transcript)
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
        compacted = _degraded_compact("Reactive compact — summary unavailable", messages, transcript)
    else:
        compacted = _build_compacted("Reactive compact", summary, messages, transcript)
    if binding is not None:
        record_compact_boundary("reactive", estimate_size(messages), transcript, compacted, binding=binding)
    return compacted


def prepare_context(messages: list, *, binding=None) -> list:
    """
    Keep sent history append-only until a real compaction checkpoint.

    Tool outputs are bounded before they enter history. Progressively rewriting
    old messages here would invalidate the exact prefix reused by provider
    prompt caches on every tool round.
    """
    # Claude Code–style checkpoint: estimate_tokens ≳ 0.835 × context_window.
    if should_autocompact(messages):
        messages[:] = compact_history(messages, binding=binding)
    return messages
