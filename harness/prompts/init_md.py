"""/init: generate or improve the project handbook (HARNESS.md / AGENTS.md).

Design (see docs/agent-knowledge-design.md, modeled on opencode / Claude Code):

- a read-only ``explore`` subagent scans the repository and produces the
  handbook content — build/test commands, architecture that is not obvious
  from filenames, conventions and gotchas;
- when a handbook already exists, its full content is fed back in and the
  agent is asked to keep everything useful, fix outdated commands, and add
  missing sections (in-place improvement, never a blind replace);
- the harness writes the file (this is a deliberate CLI action by the user),
  backing up the previous version under ``.local/init-backup/`` first.

The loading side stays ``harness/prompts/project_md.py`` (startup injection,
12k truncation) — this module only creates/updates the file.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from harness.settings import get_workdir

#: Handbook candidates, same preference as the loader (project_md.py).
TARGET_NAMES = ("HARNESS.md", "AGENTS.md")
#: Read-only subagent used to scan the repository.
INIT_AGENT = "explore"
#: Cap on the existing handbook text fed to the agent (same budget as loader).
MAX_EXISTING_CHARS = 12_000
#: Backup directory for the previous handbook version (gitignored).
BACKUP_DIRNAME = Path(".local") / "init-backup"

_AGENT_HEADER = re.compile(r"^\[[^\]]+\] [^\n]*\n*")


@dataclass(frozen=True)
class InitResult:
    path: Path
    created: bool  # False when an existing handbook was improved
    old_chars: int
    new_chars: int
    backup_path: Path | None
    note: str


def find_existing_handbook(workspace: Path | None = None) -> Path | None:
    """HARNESS.md wins over AGENTS.md (matches the loader's preference)."""
    root = Path(workspace or get_workdir())
    for name in TARGET_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _strip_agent_header(text: str) -> str:
    """Remove the `[explore / model] description (N tools, Xs)` prefix that
    run_agent_task prepends to the final text."""
    stripped = text.lstrip()
    if _AGENT_HEADER.match(stripped):
        stripped = _AGENT_HEADER.sub("", stripped, count=1)
    return stripped.strip()


def build_init_prompt(existing_text: str, workspace: Path) -> str:
    """Prompt for the read-only scanning subagent."""
    if existing_text:
        return (
            "Improve this repository's project handbook (HARNESS.md). "
            "Scan the repository to verify every claim below.\n\n"
            "Rules:\n"
            "- KEEP everything in the existing handbook that is still correct; "
            "do not delete useful conventions or constraints.\n"
            "- FIX outdated commands and wrong facts you find.\n"
            "- ADD what future agent sessions most likely need but the file "
            "lacks: build/lint/test commands, architecture and layout that are "
            "not obvious from filenames, project-specific conventions and "
            "gotchas, and references to other instruction files.\n"
            "- Output ONLY the complete updated markdown file content, no "
            "commentary, no code fences around the file.\n\n"
            "Current handbook:\n"
            "<existing-handbook>\n"
            f"{existing_text[:MAX_EXISTING_CHARS]}\n"
            "</existing-handbook>"
        )
    return (
        "Scan this repository and create its project handbook (HARNESS.md). "
        "Use your read-only tools to discover the codebase.\n\n"
        "Include sections future agent sessions most likely need:\n"
        "- Commands: how to install, run, test, and lint this project;\n"
        "- Layout: key directories and what they hold (only what is NOT "
        "obvious from filenames);\n"
        "- Conventions: code style, workflow rules, and gotchas;\n"
        "- Definition of Done or verification expectations, if discoverable.\n\n"
        "Be concise and concrete. If you cannot verify a command from the "
        "repository, mark it as needing confirmation.\n\n"
        "Output ONLY the complete markdown file content, no commentary, no "
        "code fences around the file."
    )


def _backup(existing: Path) -> Path | None:
    """Copy the previous handbook into .local/init-backup/ (best effort)."""
    try:
        backup_dir = existing.parent / BACKUP_DIRNAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{existing.name}.{int(time.time())}.bak"
        backup.write_text(existing.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return backup
    except OSError:
        return None


def run_init(
    workspace: Path | None = None,
    *,
    agent_runner=None,
) -> InitResult:
    """Generate or improve the handbook for ``workspace`` (default: active).

    ``agent_runner`` is injectable for zero-LLM tests; defaults to
    ``harness.agents.runner.run_agent_task``.
    """
    from harness.agents.runner import run_agent_task as _default_runner

    root = (workspace or get_workdir()).resolve()
    existing = find_existing_handbook(root)
    existing_text = ""
    if existing is not None:
        try:
            existing_text = existing.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            existing_text = ""

    prompt = build_init_prompt(existing_text, root)
    runner = agent_runner or _default_runner
    raw = runner(
        description="scan repo and write HARNESS.md handbook",
        prompt=prompt,
        agent_type=INIT_AGENT,
        cwd=str(root),
    )
    content = _strip_agent_header(raw)
    if not content:
        raise RuntimeError("init agent returned no handbook content")

    target = root / "HARNESS.md"
    old_chars = len(existing_text)
    backup = _backup(target) if existing is not None else None
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (same pattern as feature/goal stores). Windows occasionally
    # rejects os.replace on a just-written target (handle release delay) — retry.
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(prefix=".HARNESS.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        for attempt in range(3):
            try:
                os.replace(tmp_name, target)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    created = existing is None
    if created:
        note = f"Created {target.name} ({old_chars} -> {len(content)} chars)."
    else:
        note = f"Improved {target.name} in place ({old_chars} -> {len(content)} chars)."
    return InitResult(
        path=target,
        created=created,
        old_chars=old_chars,
        new_chars=len(content),
        backup_path=backup,
        note=note,
    )


def handle_init_command(workspace: Path | None = None) -> str:
    """User-facing /init handler (runs the read-only scan subagent)."""
    root = (workspace or get_workdir()).resolve()
    result = run_init(root)
    lines = [
        result.note,
        f"  Path: {result.path}",
        f"  Backup: {result.backup_path}" if result.backup_path else "",
        "  Next: edit it or say \"/init\" again after more changes to improve it.",
    ]
    return "\n".join(line for line in lines if line)
