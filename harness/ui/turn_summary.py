"""Track file mutations during one agent turn for human-facing summaries."""

from __future__ import annotations

from typing import Any

from harness.ui.tool_display import is_failure_tool_output

_FILE_MUTATORS = frozenset({"write_file", "edit_file", "patch_file"})


def mutated_path_from_tool(
    name: str,
    tool_input: dict | None,
    output: Any,
) -> str | None:
    """Return path if this tool successfully created/edited a file."""
    if name not in _FILE_MUTATORS:
        return None
    if is_failure_tool_output(output):
        return None
    path = str((tool_input or {}).get("path", "")).strip()
    return path or None


class TurnMutationTracker:
    """Collect unique file paths touched by write/edit in one agent_loop call."""

    def __init__(self) -> None:
        self._paths: list[str] = []
        self._seen: set[str] = set()

    def note(self, name: str, tool_input: dict | None, output: Any) -> None:
        path = mutated_path_from_tool(name, tool_input, output)
        if not path or path in self._seen:
            return
        self._seen.add(path)
        self._paths.append(path)

    @property
    def paths(self) -> list[str]:
        return list(self._paths)

    def clear(self) -> None:
        self._paths.clear()
        self._seen.clear()
