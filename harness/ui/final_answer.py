"""Emit the turn's final assistant answer as soon as the loop stops tools."""

from __future__ import annotations

from harness.console import terminal_print
from harness.ui import events


def assistant_text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, list):
        return [str(content)] if content else []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            continue
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            texts.append(block.text)
    return texts


def emit_final_assistant(messages: list, content) -> None:
    """Print final prose immediately and mark the last assistant message.

    Printing inside the agent loop (not only after loop returns to CLI) avoids
    «answer swallowed» when permission / encoding / interrupt noise happens
    between loop exit and ``print_turn_assistants``.
    """
    printed_any = False
    for text in assistant_text_blocks(content):
        if not (text or "").strip():
            continue
        # In JSONL event mode, print_turn_assistants emits the single final
        # assistant_message. Calling terminal_print here would route through
        # renderer.assistant and duplicate the event. Classic CLI still prints
        # immediately for the historical safety net.
        if not events.is_enabled():
            terminal_print(text)
        printed_any = True
    if printed_any and messages and messages[-1].get("role") == "assistant":
        # Only mark printed in classic CLI mode. In JSONL mode the safety-net
        # print_turn_assistants path must still emit the single assistant_message.
        if not events.is_enabled():
            messages[-1]["_ui_final_printed"] = True
