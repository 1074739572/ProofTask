"""Static checks for the HARNESS.md routing page (L1).

The routing page is the agent's landing page: it must stay short, contain
the key sections, and only link to files that actually exist. These checks
fail loudly when the page drifts from a usable state.
"""

from __future__ import annotations

from pathlib import Path

from evals.errors import EvalWarn
from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent

REQUIRED_SECTIONS = (
    "# ",
    "## Commands",
    "## Hard Constraints",
    "## Task Routing",
    "## Definition Of Done",
)

# Section headings that are allowed to be absent (optional).
OPTIONAL_SECTIONS = ("## Start Here",)


def _harness_md() -> Path:
    return ROOT / "HARNESS.md"


def _read_text() -> str:
    path = _harness_md()
    return path.read_text(encoding="utf-8", errors="replace")


def case_harness_md_exists() -> None:
    if not _harness_md().exists():
        raise EvalWarn("HARNESS.md missing — agent has no routing page")
    text = _read_text()
    assert text.strip(), "HARNESS.md is empty"


def case_harness_md_not_too_long() -> None:
    """Routing page must stay short (recommended ~100-150 lines)."""
    if not _harness_md().exists():
        raise EvalWarn("HARNESS.md missing — skipping length check")
    lines = len(_read_text().splitlines())
    assert lines <= 200, f"HARNESS.md is {lines} lines — too long for a routing page"


def case_harness_md_has_key_sections() -> None:
    if not _harness_md().exists():
        raise EvalWarn("HARNESS.md missing — skipping sections check")
    text = _read_text()
    missing = [s for s in REQUIRED_SECTIONS if s not in text]
    assert not missing, f"HARNESS.md missing sections: {missing}"


def case_harness_md_links_exist() -> None:
    """Every markdown link target in HARNESS.md must resolve to a real file."""
    import re

    if not _harness_md().exists():
        raise EvalWarn("HARNESS.md missing — skipping link check")
    text = _read_text()
    # inline links: [label](target) — these must resolve.
    links = re.findall(r"\]\(([^)#]+)\)", text)
    broken = []
    for target in links:
        target = target.strip()
        if not target or target.startswith("http") or target.startswith("#"):
            continue
        candidate = ROOT / target
        if not candidate.exists():
            broken.append(target)
    assert not broken, f"HARNESS.md has broken links: {broken}"


def case_harness_md_has_real_commands() -> None:
    """Commands section should reference real, runnable commands (not placeholders)."""
    if not _harness_md().exists():
        raise EvalWarn("HARNESS.md missing — skipping commands check")
    text = _read_text()
    lower = text.lower()
    assert "python main.py" in lower or "python -m" in lower, (
        "HARNESS.md Commands section has no real runnable command"
    )


CASES = [
    EvalCase(
        "project_md.exists",
        "HARNESS.md routing page exists",
        "project_md",
        case_harness_md_exists,
    ),
    EvalCase(
        "project_md.length",
        "HARNESS.md stays within routing-page length",
        "project_md",
        case_harness_md_not_too_long,
    ),
    EvalCase(
        "project_md.sections",
        "HARNESS.md has key sections",
        "project_md",
        case_harness_md_has_key_sections,
    ),
    EvalCase(
        "project_md.links",
        "HARNESS.md links all resolve",
        "project_md",
        case_harness_md_links_exist,
    ),
    EvalCase(
        "project_md.commands",
        "HARNESS.md has real runnable commands",
        "project_md",
        case_harness_md_has_real_commands,
    ),
]
