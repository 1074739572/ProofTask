"""Lightweight pending-question state for ordinary (non-Goal) turns."""

from __future__ import annotations

import re

from harness.messages.blocks import block_text, is_text

_QUESTION_END_RE = re.compile(
    r"(?:\?|？|吗[。.!！?？]?|呢[。.!！?？]?|哪一个[。.!！?？]?)$",
    re.IGNORECASE,
)


def _assistant_text(messages: list, start: int) -> str:
    for message in reversed(messages[max(0, start):]):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "\n".join(
                block_text(block).strip()
                for block in content
                if is_text(block) and block_text(block).strip()
            ).strip()
            if text:
                return text
    return ""


def question_from_turn(messages: list, turn_start: int) -> str | None:
    """Extract only a likely user-facing question from the latest turn."""
    text = _assistant_text(messages, turn_start)
    if not text or len(text) > 800:
        return None
    # Keep the final line/sentence so a long answer ending in a question does
    # not make the whole response the pending prompt.
    candidate = re.split(r"\n+|(?<=[。.!！?？])\s+", text)[-1].strip()
    if not _QUESTION_END_RE.search(candidate):
        return None
    return candidate[:500]


def remember_turn_question(context: dict, messages: list, turn_start: int) -> None:
    question = question_from_turn(messages, turn_start)
    if question:
        context["pending_question"] = {"text": question, "turn": len(messages)}
    else:
        context.pop("pending_question", None)


def remember_latest_question(context: dict, messages: list) -> None:
    """Restore a pending question after a process restart from saved history."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            start = index
            break
    remember_turn_question(context, messages, start)


def pending_question_text(context: dict) -> str:
    value = context.get("pending_question")
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def consume_pending_question(context: dict) -> str | None:
    question = pending_question_text(context)
    context.pop("pending_question", None)
    return question or None
