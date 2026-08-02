"""Append-only JSONL mailboxes for multi-agent communication."""

from __future__ import annotations

import json
import time

from harness.console import terminal_print
from harness.settings import MAILBOX_DIR

active_teammates: dict[str, bool] = {}


class MessageBus:
    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict | None = None,
    ) -> None:
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {},
        }
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(msg) + "\n")
        terminal_print(
            f"  \033[33m[bus] {from_agent} → {to_agent}: "
            f"({msg_type}) {content[:50]}\033[0m"
        )

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [
            json.loads(line)
            for line in inbox.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        inbox.unlink()
        return msgs


BUS = MessageBus()
