"""Stable system prompt — kept identical across LLM calls when skills catalog unchanged."""

from __future__ import annotations

from harness.prompts.sections import PROMPT_SECTIONS
from harness.settings import get_workdir
from harness.skills_loader import list_skills


def assemble_static_system_prompt() -> str:
    """Identity, tools, workspace, and skills catalog only (no per-turn session state).

    The workspace line follows the *active* workspace (``get_workdir()``) so a
    running process that switched projects via ``/open`` advertises the new
    directory to the model instead of the startup directory.
    """
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["grounding"],
        PROMPT_SECTIONS["tools"],
        f"Working directory: {get_workdir()}",
        (
            "Skills catalog:\n"
            + list_skills()
            + "\nUse load_skill(name) when a skill is relevant."
        ),
    ]
    return "\n\n".join(sections)
