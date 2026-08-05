"""M006 regression: TUI /open must reload the NEW project's HARNESS.md.

The event-stream /open path previously kept the OLD context (and old project
instructions + history), so after switching projects the agent still saw the
previous project's rules. This mirrors the CLI path (clear history, reload
project instructions) for the TUI backend.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from harness.event_stream import _run_user_turn
from harness.prompts.project_md import apply_project_instructions, find_project_md


def _make_project(root: Path, name: str, test_cmd: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "HARNESS.md").write_text(
        f"# {name}\n\n## Commands\n- Test: {test_cmd}\n",
        encoding="utf-8",
    )


def test_open_reloads_project_instructions(tmp_path, monkeypatch):
    """After /open, session context must carry the NEW project's HARNESS.md."""
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    _make_project(proj_a, "ProjectA", "pytest a")
    _make_project(proj_b, "ProjectB", "pytest b")

    # ---- simulate: start in A, inject its instructions ----
    history: list = []
    context = {}
    apply_project_instructions(context, start=proj_a)
    assert "pytest a" in context["project_instructions"]

    # ---- simulate TUI /open to B (monkeypatch switch so it doesn't touch real cwd) ----
    from harness import settings as settings_mod

    def _fake_switch(target):
        target = Path(target).resolve()
        settings_mod._workspace = target
        settings_mod._workspace_generation += 1
        return True, f"Switched workspace → {target}", None

    monkeypatch.setattr(settings_mod, "switch_workspace", _fake_switch)
    # keep _handle_open_workspace from writing recent projects
    monkeypatch.setattr(
        "harness.workspace.record_recent_project", lambda *a, **k: None
    )
    monkeypatch.chdir(proj_b)

    with mock.patch("harness.event_stream.emit") as mock_emit:
        context, _interrupted, _binding = _run_user_turn(
            f"/open {proj_b}", history, context, None
        )

    # The context must now carry B's instructions (not A's).
    text = context.get("project_instructions", "")
    assert "pytest b" in text, f"expected B's instructions after /open, got: {text[:200]!r}"
    assert "pytest a" not in text, "old project A instructions leaked after /open"

    # History should be cleared (fresh session in new project).
    assert history == [], f"expected history cleared after /open, got {len(history)} msgs"

    # Restore cwd (Windows: cannot delete a dir that is the cwd).
    monkeypatch.chdir(tmp_path)
