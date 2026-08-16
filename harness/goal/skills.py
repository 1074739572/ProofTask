"""Bounded workflow-skill support for isolated Goal agents."""

from __future__ import annotations

from typing import Iterable

from harness.skills_loader import load_skill, skill_names

MAX_ASSIGNED_SKILLS = 2
MAX_ASSIGNED_SKILL_BYTES = 12_000


def normalize_goal_skills(names: Iterable[object]) -> list[str]:
    """Return installed, unique skill ids suitable for one Goal Task."""
    installed = set(skill_names())
    selected: list[str] = []
    for value in names:
        name = str(value or "").strip()
        if not name or name not in installed or name in selected:
            continue
        selected.append(name)
        if len(selected) >= MAX_ASSIGNED_SKILLS:
            break
    return selected


def assigned_skill_context(names: Iterable[object]) -> str:
    """Preload selected guidance without allowing unbounded prompt growth."""
    chunks: list[str] = []
    used = 0
    for name in normalize_goal_skills(names):
        body = load_skill(name)
        encoded = body.encode("utf-8")
        remaining = MAX_ASSIGNED_SKILL_BYTES - used
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            body = encoded[:remaining].decode("utf-8", errors="ignore")
            body += "\n[assigned skill truncated]"
        chunks.append(f"--- Assigned skill: {name} ---\n{body}")
        used += len(body.encode("utf-8"))
    return "\n\n".join(chunks)
