"""Resolve Goal evidence hints into a finite, secret-free read-only file list."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from harness.goal.discovery import EXCLUDED_DIRS, EXCLUDED_NAMES, MAX_FILE_BYTES, TEXT_SUFFIXES


def bounded_read_paths(
    workspace: str | Path,
    candidates: Iterable[str | Path],
    *,
    limit: int = 96,
) -> tuple[str, ...]:
    root = Path(workspace).expanduser().resolve()
    found: list[str] = []
    excluded = EXCLUDED_DIRS | {".features", ".agents", ".codex", "__pycache__", "secrets", "secret", "cache"}

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(root)
            if not relative.parts or any(
                part.casefold() in excluded or part.casefold().startswith(".env")
                for part in relative.parts
            ):
                return
            if not resolved.is_file() or resolved.name in EXCLUDED_NAMES:
                return
            if resolved.suffix.lower() not in TEXT_SUFFIXES or resolved.stat().st_size > MAX_FILE_BYTES:
                return
            name = relative.as_posix()
            if name not in found:
                found.append(name)
        except (OSError, ValueError):
            return

    for raw in candidates:
        if len(found) >= limit:
            break
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            candidate = Path(value).expanduser()
            candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
            relative = candidate.relative_to(root)
            if not relative.parts or any(
                part.casefold() in excluded or part.casefold().startswith(".env")
                for part in relative.parts
            ):
                continue
            if candidate.is_dir():
                for path in sorted(candidate.rglob("*")):
                    add(path)
                    if len(found) >= limit:
                        break
            else:
                add(candidate)
        except (OSError, ValueError):
            continue
    return tuple(found)
