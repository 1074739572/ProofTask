"""Hard guardrails for writing-mode RAG workflows."""

from __future__ import annotations

import os
from pathlib import PurePosixPath


def writing_guard_enabled() -> bool:
    raw = os.getenv("HARNESS_WRITING_GUARD", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _norm_path(path: str) -> str:
    return str(path or "").replace("\\", "/")


def is_output_prose_path(path: str) -> bool:
    """True for report drafts under output/ (not code elsewhere)."""
    norm = _norm_path(path).lstrip("./")
    if not norm.startswith("output/"):
        return False
    suffix = PurePosixPath(norm).suffix.lower()
    return suffix in ("", ".md", ".markdown", ".txt")


class WritingGuard:
    """Require rag_search before writing report prose to output/."""

    def __init__(self, *, active: bool) -> None:
        self.active = active and writing_guard_enabled()
        self.rag_search_count = 0

    def note_tool(self, name: str) -> None:
        if self.active and name == "rag_search":
            self.rag_search_count += 1

    def check_write(self, name: str, tool_input: dict | None) -> tuple[bool, str]:
        if not self.active:
            return False, ""
        if name not in ("write_file", "edit_file", "patch_file"):
            return False, ""
        path = (tool_input or {}).get("path", "")
        if not is_output_prose_path(str(path)):
            return False, ""
        if self.rag_search_count >= 1:
            return False, ""
        return True, (
            "[WritingGuard] Blocked: writing mode requires rag_search on local "
            f"reference docs before {name} to `{path}`. "
            "Run rag_search with section/structure queries from files/样例, "
            "then draft into output/. Do not read_file entire docx references."
        )
