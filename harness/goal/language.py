"""Language selection for human-facing Goal artifacts."""

from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def detect_goal_language(text: str) -> str:
    """Choose a stable display language from the user's Goal request."""
    return "zh-CN" if _CJK_RE.search(str(text or "")) else "en"


def human_language_label(language: str | None) -> str:
    """Return an explicit model instruction, never a translated JSON schema."""
    return "Simplified Chinese" if language == "zh-CN" else "English"
