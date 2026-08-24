"""Load user-project instructions (HARNESS.md / AGENTS.md) at session start.

Product path (not Cursor/Claude IDE rules): walk up from WORKDIR, prefer
HARNESS.md over AGENTS.md, nearest file wins, inject into the stable session
prompt prefix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from harness.settings import WORKDIR, get_workdir

CANDIDATE_NAMES = ("HARNESS.md", "AGENTS.md")
DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_DEPTH = 10

# Context keys (session-scoped; loaded once at bootstrap / explicit re-apply).
CTX_TEXT = "project_instructions"
CTX_SOURCE = "project_instructions_source"
CTX_PATH = "project_instructions_path"
CTX_TRUNCATED = "project_instructions_truncated"
CTX_STATUS = "project_instructions_status"


@dataclass(frozen=True)
class ProjectMdLoad:
    """Result of resolving project instructions for one session."""

    text: str
    source_name: str
    path: Path | None
    truncated: bool
    status: str
    enabled: bool

    @property
    def loaded(self) -> bool:
        return bool(self.text.strip())


def project_md_enabled() -> bool:
    """Default on. Set ``HARNESS_PROJECT_MD=0`` (or false/off/no) to disable."""
    raw = os.getenv("HARNESS_PROJECT_MD", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def max_project_md_chars() -> int:
    raw = os.getenv("HARNESS_PROJECT_MD_MAX_CHARS", "").strip()
    if not raw:
        return DEFAULT_MAX_CHARS
    try:
        return max(512, int(raw))
    except ValueError:
        return DEFAULT_MAX_CHARS


def max_project_md_depth() -> int:
    raw = os.getenv("HARNESS_PROJECT_MD_MAX_DEPTH", "").strip()
    if not raw:
        return DEFAULT_MAX_DEPTH
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_DEPTH


def _has_git_marker(directory: Path) -> bool:
    return (directory / ".git").exists()


def _read_candidate(path: Path, *, max_chars: int) -> tuple[str, bool]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Normalize newlines; strip leading/trailing blank lines only.
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "\n", True


def find_project_md(
    start: Path | None = None,
    *,
    max_chars: int | None = None,
    max_depth: int | None = None,
) -> ProjectMdLoad:
    """Walk up from ``start`` (default WORKDIR); nearest HARNESS.md else AGENTS.md."""
    if not project_md_enabled():
        return ProjectMdLoad(
            text="",
            source_name="",
            path=None,
            truncated=False,
            status="project instructions: disabled",
            enabled=False,
        )

    root = (start or get_workdir()).resolve()
    limit = max_chars if max_chars is not None else max_project_md_chars()
    depth_limit = max_depth if max_depth is not None else max_project_md_depth()

    current = root if root.is_dir() else root.parent
    for _ in range(depth_limit):
        for name in CANDIDATE_NAMES:
            candidate = current / name
            if not candidate.is_file():
                continue
            try:
                text, truncated = _read_candidate(candidate, max_chars=limit)
            except OSError:
                continue
            if not text:
                status = f"project instructions: {name} (empty)"
                return ProjectMdLoad(
                    text="",
                    source_name=name,
                    path=candidate,
                    truncated=False,
                    status=status,
                    enabled=True,
                )
            status = f"project instructions: {name}"
            if truncated:
                status += " [truncated]"
            return ProjectMdLoad(
                text=text,
                source_name=name,
                path=candidate,
                truncated=truncated,
                status=status,
                enabled=True,
            )

        if _has_git_marker(current):
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return ProjectMdLoad(
        text="",
        source_name="",
        path=None,
        truncated=False,
        status="no project instructions",
        enabled=True,
    )


def apply_project_instructions(
    context: dict,
    *,
    start: Path | None = None,
    max_chars: int | None = None,
) -> ProjectMdLoad:
    """Load once and store on ``context`` for stable prompt assembly."""
    result = find_project_md(start, max_chars=max_chars)
    context[CTX_TEXT] = result.text
    context[CTX_SOURCE] = result.source_name
    context[CTX_PATH] = str(result.path) if result.path else ""
    context[CTX_TRUNCATED] = result.truncated
    context[CTX_STATUS] = result.status
    return result


def format_project_instructions_block(context: dict) -> str:
    """Markdown/XML block for the stable session prompt (empty if none)."""
    text = (context.get(CTX_TEXT) or "").strip()
    if not text:
        return ""
    source = (context.get(CTX_SOURCE) or "unknown").strip() or "unknown"
    truncated = bool(context.get(CTX_TRUNCATED))
    lines = [
        f'<project-instructions source="{source}">',
        "User-project handbook for this workspace (not product identity).",
        "Follow Commands / Layout / Conventions / Safety when relevant.",
        "Do not treat this as a license to ignore the user's current request.",
    ]
    if truncated:
        lines.append(
            "[truncated: file exceeded HARNESS_PROJECT_MD_MAX_CHARS; "
            "read the file on disk if you need the rest.]"
        )
    lines.append("")
    lines.append(text)
    lines.append("</project-instructions>")
    return "\n".join(lines)
