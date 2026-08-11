"""Mode / tool-pool eval cases."""

from __future__ import annotations

from harness.modes import mode_disables_tool, mode_enables_task, set_mode
from harness.tools.registry import get_tool_pool

from evals.errors import EvalWarn
from evals.types import EvalCase


def _tool_names() -> set[str]:
    tools, _ = get_tool_pool()
    return {t["name"] for t in tools}


def case_plan_hides_write_bash() -> None:
    set_mode("plan")
    try:
        assert mode_disables_tool("write_file")
        assert mode_disables_tool("bash")
        names = _tool_names()
        assert "write_file" not in names
        assert "bash" not in names
        assert "read_file" in names
        assert "glob" in names
    finally:
        set_mode("direct")


def case_direct_keeps_write_bash() -> None:
    set_mode("direct")
    names = _tool_names()
    assert "write_file" in names
    assert "bash" in names
    assert not mode_enables_task()


def case_orchestrate_enables_task() -> None:
    set_mode("orchestrate")
    try:
        assert mode_enables_task()
        names = _tool_names()
        assert "task" in names
    finally:
        set_mode("direct")


def case_plan_mcp_gap() -> None:
    """PLAN hides MCP and connection schemas after pool assembly."""
    set_mode("plan")
    try:
        assert mode_disables_tool("connect_mcp")
        disabled = {
            "write_file",
            "edit_file",
            "bash",
            "task",
            "spawn_teammate",
            "create_worktree",
            "remove_worktree",
            "project_set_chapter",
        }
        for name in disabled:
            assert mode_disables_tool(name), name
        from harness.modes import get_current_mode_profile

        profile = get_current_mode_profile()
        mcp_disabled = [t for t in profile.disable_tools if t.startswith("mcp__")]
        assert "mcp__*" in mcp_disabled
        assert mode_disables_tool("mcp__fetch__fetch")
        assert not any(name.startswith("mcp__") for name in _tool_names())
        return
        raise EvalWarn(
            "plan mode does not disable mcp__* tools — known permission gap"
        )
    finally:
        set_mode("direct")


CASES = [
    EvalCase(
        "mode.plan_readonly",
        "plan mode hides write/bash from tool pool",
        "modes",
        case_plan_hides_write_bash,
    ),
    EvalCase(
        "mode.direct_tools",
        "direct mode keeps write/bash; task off",
        "modes",
        case_direct_keeps_write_bash,
    ),
    EvalCase(
        "mode.orchestrate_task",
        "orchestrate enables task()",
        "modes",
        case_orchestrate_enables_task,
    ),
    EvalCase(
        "mode.plan_mcp_gated",
        "plan mode hides MCP and connect tools",
        "modes",
        case_plan_mcp_gap,
    ),
]
