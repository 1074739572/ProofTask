from __future__ import annotations

from pathlib import Path
from unittest import mock


def _write_skill(root: Path, directory: str, body: str, *, name: str | None = None) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    skill_name = name or directory
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test skill\n---\n{body}",
        encoding="utf-8",
    )


def test_skill_scan_is_workspace_aware_and_project_overrides_builtin(tmp_path, monkeypatch):
    from harness import settings
    from harness import skills_loader

    project_skills = tmp_path / "skills"
    _write_skill(project_skills, "demo", "project body")
    monkeypatch.setattr(settings, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(settings, "workspace_generation", lambda: 42)

    skills_loader.scan_skills()

    assert skills_loader.SKILL_REGISTRY["demo"]["source"] == "project"
    assert "project body" in skills_loader.load_skill("demo")


def test_skill_scan_records_same_name_conflicts(tmp_path, monkeypatch):
    from harness import settings, skills_loader

    _write_skill(tmp_path / "skills", "demo", "project body")
    builtin = tmp_path / "builtin"
    _write_skill(builtin, "demo", "builtin body", name="demo")
    monkeypatch.setattr(settings, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(settings, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(settings, "workspace_generation", lambda: 420)
    skills_loader.scan_skills()
    assert "demo" in skills_loader.SKILL_CONFLICTS
    assert len(skills_loader.SKILL_CONFLICTS["demo"]) == 2


def test_skill_scan_isolates_bad_encoding_and_scalar_frontmatter(tmp_path, monkeypatch):
    from harness import settings
    from harness import skills_loader

    _write_skill(tmp_path / "skills", "good", "good body")
    bad = tmp_path / "skills" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_bytes(b"\xff\xfe")
    scalar = tmp_path / "skills" / "scalar"
    scalar.mkdir(parents=True)
    (scalar / "SKILL.md").write_text("---\njust-a-scalar\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(settings, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(settings, "workspace_generation", lambda: 43)

    skills_loader.scan_skills()

    assert "good" in skills_loader.SKILL_REGISTRY
    assert "bad" not in skills_loader.SKILL_REGISTRY
    assert "scalar" in skills_loader.SKILL_REGISTRY


def test_load_skill_is_bounded_and_does_not_bootstrap_rag(tmp_path, monkeypatch):
    from harness import settings
    from harness import skills_loader

    _write_skill(tmp_path / "skills", "thesis-writing", "x" * 20_000)
    monkeypatch.setattr(settings, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(settings, "workspace_generation", lambda: 44)
    skills_loader.scan_skills()

    with mock.patch("harness.rag.bootstrap.ensure_rag_indexed") as ensure:
        content = skills_loader.load_skill("thesis-writing")

    ensure.assert_not_called()
    assert len(content.encode("utf-8")) <= skills_loader.MAX_SKILL_BODY_BYTES + 100
    assert "truncated" in content


def test_read_only_modes_hide_mutating_tools():
    from harness.modes import set_mode
    from harness.tools.registry import get_tool_pool

    forbidden = {
        "write_file",
        "edit_file",
        "patch_file",
        "bash",
        "create_task",
        "claim_task",
        "complete_task",
        "project_init",
        "project_note",
        "project_reset",
        "rag_index",
        "connect_mcp",
    }
    try:
        for mode in ("plan", "lookup", "file", "grill"):
            set_mode(mode)
            tools, handlers = get_tool_pool()
            names = {tool["name"] for tool in tools} | set(handlers)
            assert not (names & forbidden), (mode, names & forbidden)
            assert {"search_text", "inspect_file", "git_status", "git_diff"} <= names
    finally:
        set_mode("direct")


def test_global_safe_file_tools_are_registered_with_handlers():
    from harness.modes import set_mode
    from harness.tools.registry import get_tool_pool

    set_mode("direct")
    tools, handlers = get_tool_pool()
    names = {tool["name"] for tool in tools}
    assert {"search_text", "patch_file", "inspect_file", "git_status", "git_diff"} <= names
    assert {"search_text", "patch_file", "inspect_file", "git_status", "git_diff"} <= set(handlers)


def test_mcp_name_collision_is_reported_and_skipped():
    from harness.mcp import pool

    class Client:
        tools = [
            {"name": "a.b", "description": "first", "inputSchema": {"type": "object"}},
            {"name": "a_b", "description": "second", "inputSchema": {"type": "object"}},
        ]

        def call_tool(self, name, args):
            return name

    old_clients = dict(pool.mcp_clients)
    try:
        pool.mcp_clients.clear()
        pool.mcp_clients["server"] = Client()
        tools, handlers = pool.assemble_tool_pool([], {})
        assert len(tools) == 1
        assert len(handlers) == 1
        assert any("collision" in warning for warning in pool.mcp_pool_warnings)
    finally:
        pool.mcp_clients.clear()
        pool.mcp_clients.update(old_clients)
