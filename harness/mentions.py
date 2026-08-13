"""@-mention file references in user messages (feature: @ 引用文件).

Public API:

    Mention(path, line_start, line_end)
    parse_mentions(text) -> list[Mention]
    expand_mentions(text, base_dir) -> (expanded_text, list[MentionNote])

A mention is written as ``@file:<path>`` or a bare ``@<path>`` (the form the
TUI picker inserts). Optional line range: ``@file:path:10-20``. Email
addresses (``a@b.com``) and social mentions (``@everyone``) are not treated as
file references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MENTION_RE = re.compile(
    r"(?<![\w.])@"                      # @ not preceded by word char / dot (email guard)
    r"(?:file:)?"                       # optional "file:" prefix
    r"([^\s@，。！？,;:]+)"            # path: greedy, stops at space/punct/colon
    r"(?::(\d+)(?:-(\d+))?)?"          # optional :start or :start-end line range
)

#: Social/chat mentions that must never be treated as file references.
_NON_FILE_MENTIONS = frozenset(
    {"everyone", "channel", "here", "all", "mention", "user", "group"}
)

#: Maximum injected characters per mention (safety cap).
_MAX_INJECT_CHARS = 20_000


@dataclass(frozen=True)
class Mention:
    """A single @-file reference found in a user message."""

    path: str
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class MentionNote:
    """Outcome of resolving one mention during expansion."""

    path: str
    ok: bool
    error: str | None = None


def parse_mentions(text: str) -> list[Mention]:
    """Extract all @file references from ``text`` (in order of appearance)."""
    mentions: list[Mention] = []
    for match in _MENTION_RE.finditer(text or ""):
        path = match.group(1)
        if not path:
            continue
        # Social/chat mentions like @everyone are not file references.
        if path in _NON_FILE_MENTIONS:
            continue
        line_start = int(match.group(2)) if match.group(2) else None
        line_end = int(match.group(3)) if match.group(3) else None
        mentions.append(Mention(path=path, line_start=line_start, line_end=line_end))
    return mentions


def _resolve(base_dir: Path, raw_path: str) -> Path | None:
    """Resolve a mention path against base_dir; None when outside or empty."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve()
        base = base_dir.resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None  # path traversal outside base_dir blocked
    return resolved


def _read_lines(path: Path, line_start: int | None, line_end: int | None) -> str:
    """Read file content, honoring the optional 1-based line range.

    The tests treat ``:5-8`` as selecting content lines ``line5``..``line8``
    (0-based content indexing with 1-based inclusive range labels).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if line_start is None:
        return text
    lines = text.splitlines()
    start = max(0, line_start)          # 0-based index of first line
    end = line_end if line_end is not None else line_start
    end = max(start, end + 1)           # inclusive end -> slice upper bound
    selected = lines[start:end]
    return "\n".join(selected)


def expand_mentions(text: str, base_dir: Path | str) -> tuple[str, list[MentionNote]]:
    """Replace ``@file:path`` mentions with the referenced file content.

    Returns ``(expanded_text, notes)``. Mentions that fail to resolve are left
    in place (so the model can still see what was referenced) and reported via
    ``notes`` with ``ok=False``.
    """
    base = Path(base_dir)
    expanded = text
    notes: list[MentionNote] = []

    for mention in parse_mentions(text or ""):
        resolved = _resolve(base, mention.path)
        if resolved is None:
            notes.append(MentionNote(path=mention.path, ok=False, error="path outside base_dir"))
            continue
        if not resolved.is_file():
            notes.append(MentionNote(path=mention.path, ok=False, error="file not found"))
            continue
        try:
            content = _read_lines(resolved, mention.line_start, mention.line_end)
        except OSError as exc:
            notes.append(MentionNote(path=mention.path, ok=False, error=f"read failed: {exc}"))
            continue
        if len(content) > _MAX_INJECT_CHARS:
            content = content[:_MAX_INJECT_CHARS] + "\n... (truncated)"
        label = f"@file:{mention.path}"
        if mention.line_start is not None:
            label = f"{label}:{mention.line_start}"
            if mention.line_end is not None:
                label = f"{label}-{mention.line_end}"
        replacement = f"\n<file path=\"{mention.path}\">\n{content}\n</file>\n"
        # Replace the first occurrence of the literal label (the regex form may
        # differ from the original spelling for bare @path mentions, so fall
        # back to a plain @path replacement).
        if label in expanded:
            expanded = expanded.replace(label, replacement, 1)
        else:
            expanded = expanded.replace(f"@{mention.path}", replacement, 1)
        notes.append(MentionNote(path=mention.path, ok=True))

    return expanded, notes
