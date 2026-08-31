"""Path completion core shared by CLI input adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class PathCompletionContext:
    start: int
    end: int
    value: str
    directories_only: bool


def path_completion_context(
    text: str, cursor_pos: int | None = None
) -> PathCompletionContext | None:
    end = len(text) if cursor_pos is None else max(0, min(cursor_pos, len(text)))
    before_cursor = text[:end]

    open_match = re.match(r"^\s*/open(?:\s+|$)", before_cursor, re.IGNORECASE)
    if open_match:
        return PathCompletionContext(open_match.end(), end, before_cursor[open_match.end() :], True)

    at_match = None
    for match in re.finditer(r"@", before_cursor):
        at_match = match
    if at_match is None:
        return None
    start = at_match.end()
    return PathCompletionContext(start, end, before_cursor[start:], False)


def complete_paths(
    text: str,
    cursor_pos: int | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> list[str]:
    context = path_completion_context(text, cursor_pos)
    if context is None:
        return []

    typed = context.value
    separator = _preferred_separator(typed)
    directory_text, prefix = _split_typed_path(typed)
    lookup_directory = _lookup_directory(directory_text, cwd=cwd)
    try:
        entries = sorted(lookup_directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []

    ranked: list[tuple[int, str, str]] = []
    for entry in entries:
        score = _fuzzy_match_score(prefix, entry.name)
        if score is None:
            continue
        if context.directories_only and not entry.is_dir():
            continue
        completed_path = directory_text + entry.name
        if entry.is_dir():
            completed_path += separator
        ranked.append((score, entry.name.casefold(), text[: context.start] + completed_path + text[context.end :]))
    # Prefix matches remain first; fuzzy subsequence matches make deep paths
    # discoverable without changing the wire format or replacement semantics.
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def _fuzzy_match_score(query: str, candidate: str) -> int | None:
    """Return a stable ranking score when *query* is a candidate subsequence.

    A score of zero is reserved for ordinary prefix matches. Fuzzy matches are
    penalized by gaps and by a non-prefix start, while still remaining
    deterministic for equal scores. Empty queries match every candidate.
    """
    needle = query.casefold()
    haystack = candidate.casefold()
    if not needle:
        return 0
    if haystack.startswith(needle):
        return 0
    pos = 0
    first = -1
    gaps = 0
    for char in needle:
        found = haystack.find(char, pos)
        if found < 0:
            return None
        if first < 0:
            first = found
        gaps += max(0, found - pos)
        pos = found + 1
    return 10 + first + gaps


def _preferred_separator(value: str) -> str:
    slash = value.rfind("/")
    backslash = value.rfind("\\")
    if slash >= 0 or backslash >= 0:
        return "/" if slash > backslash else "\\"
    return os.sep


def _split_typed_path(value: str) -> tuple[str, str]:
    split_at = max(value.rfind("/"), value.rfind("\\"))
    if split_at < 0:
        return "", value
    return value[: split_at + 1], value[split_at + 1 :]


def _lookup_directory(
    directory_text: str, *, cwd: str | os.PathLike[str] | None
) -> Path:
    normalized = directory_text.replace("\\", os.sep).replace("/", os.sep)
    expanded = Path(normalized).expanduser() if normalized else Path()
    if expanded.is_absolute():
        return expanded
    return Path(cwd) / expanded if cwd is not None else Path.cwd() / expanded


class ReadlinePathCompleter:
    """Readline adapter that computes candidates from the full input line."""

    def __init__(self, readline_module) -> None:
        self.readline = readline_module
        self._line = None
        self._matches: list[str] = []

    def __call__(self, _text: str, state: int) -> str | None:
        line = self.readline.get_line_buffer()
        cursor = self.readline.get_endidx()
        key = (line, cursor)
        if state == 0 or key != self._line:
            self._line = key
            self._matches = complete_paths(line, cursor)
        if state < 0 or state >= len(self._matches):
            return None
        return self._matches[state]
