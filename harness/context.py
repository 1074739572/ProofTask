"""Per-turn runtime context for prompt assembly."""

from __future__ import annotations

from harness.mcp.pool import mcp_clients
from harness.settings import get_workspace_paths
from harness.teams.bus import active_teammates


def update_context(context: dict, messages: list) -> dict:
    memories = context.get("memories")
    if memories is None:
        memories = ""
        memory_index = get_workspace_paths().memory_index
        if memory_index.exists():
            # Snapshot memory at session/open time. Re-reading it on every tool
            # round unnecessarily changes prompt input and defeats caching.
            memories = memory_index.read_text(encoding="utf-8", errors="replace")[:2000]
    return {
        **context,
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }
