"""Static checks for the HARNESS.md routing page (L1) + /init generator (I).

The routing page is the agent's landing page: it must stay short, contain
the key sections, and only link to files that actually exist. These checks
fail loudly when the page drifts from a usable state.

I-series (/init, zero-LLM): the scanning subagent is mocked; the harness
write/backup/merge behavior is what is asserted. Runs in temp workspaces.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

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


# --- I-series: /init generator (zero-LLM, mocked agent) -----------------------

_FAKE_HANDBOOK = (
    "# Demo Project\n"
    "## Commands\n"
    "- Test: pytest -q\n"
    "## Layout\n"
    "- src/ core code\n"
)


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


def _fake_runner(*args, **kwargs):
    return f"[explore / mock] scanned (1 tools, 0.0s)\n\n{_FAKE_HANDBOOK}"


def case_i001_init_creates_handbook() -> None:
    """/init creates HARNESS.md when none exists (agent output written)."""
    from harness.prompts.init_md import run_init

    tmp, ws = _tmp_workspace()
    try:
        result = run_init(ws, agent_runner=_fake_runner)
        assert result.created
        target = ws / "HARNESS.md"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == _FAKE_HANDBOOK.strip()
        assert result.path == target
        assert result.backup_path is None
    finally:
        tmp.cleanup()


def case_i002_init_improves_in_place() -> None:
    """/init improves an existing HARNESS.md in place and backs it up."""
    from harness.prompts.init_md import run_init

    tmp, ws = _tmp_workspace()
    try:
        existing = "# Old Project\n\n## Commands\n- Test: old\n"
        (ws / "HARNESS.md").write_text(existing, encoding="utf-8")
        result = run_init(ws, agent_runner=_fake_runner)
        assert not result.created
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert result.backup_path.read_text(encoding="utf-8") == existing
        # The new content replaced the old one, and the old text is preserved
        # in the backup.
        written = (ws / "HARNESS.md").read_text(encoding="utf-8")
        assert written == _FAKE_HANDBOOK.strip()
    finally:
        tmp.cleanup()


def case_i003_init_prompt_includes_existing() -> None:
    """The improvement prompt feeds the existing handbook back to the agent."""
    from harness.prompts.init_md import build_init_prompt

    captured = {}

    def spy_runner(description, prompt, agent_type, *, cwd=None):
        captured["prompt"] = prompt
        return _fake_runner()

    from harness.prompts.init_md import run_init

    tmp, ws = _tmp_workspace()
    try:
        existing = "# Old\n\n## Commands\n- Test: pytest -q\n"
        (ws / "HARNESS.md").write_text(existing, encoding="utf-8")
        run_init(ws, agent_runner=spy_runner)
        prompt = captured["prompt"]
        assert "KEEP everything" in prompt
        assert existing in prompt
        assert "Current handbook" in prompt
    finally:
        tmp.cleanup()


def case_i004_init_empty_result_rejected() -> None:
    """An empty agent result must not clobber an existing handbook."""
    from harness.prompts.init_md import run_init

    tmp, ws = _tmp_workspace()
    try:
        existing = "# Old\n\n## Commands\n- Test: pytest -q\n"
        (ws / "HARNESS.md").write_text(existing, encoding="utf-8")
        try:
            run_init(ws, agent_runner=lambda *a, **k: "[explore / mock] nothing (0 tools, 0.0s)")
        except RuntimeError:
            pass
        else:
            raise AssertionError("empty agent output must raise")
        # Existing handbook untouched.
        assert (ws / "HARNESS.md").read_text(encoding="utf-8") == existing
    finally:
        tmp.cleanup()


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
    EvalCase(
        "init.creates_handbook",
        "I001: /init creates HARNESS.md when none exists",
        "project_md",
        case_i001_init_creates_handbook,
    ),
    EvalCase(
        "init.improves_in_place",
        "I002: /init improves existing HARNESS.md in place with a backup",
        "project_md",
        case_i002_init_improves_in_place,
    ),
    EvalCase(
        "init.prompt_includes_existing",
        "I003: improvement prompt feeds the existing handbook back",
        "project_md",
        case_i003_init_prompt_includes_existing,
    ),
    EvalCase(
        "init.empty_result_rejected",
        "I004: empty agent output never clobbers an existing handbook",
        "project_md",
        case_i004_init_empty_result_rejected,
    ),
]
