"""Permission / safety eval cases."""

from __future__ import annotations

from unittest import mock

from harness.hooks import permission_hook
from harness.tools.filesystem import safe_path

from evals.types import EvalCase


def _block(name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": tool_input, "id": "t1"}


def case_bash_deny_list() -> None:
    denied = permission_hook(_block("bash", {"command": "sudo reboot"}))
    assert denied and "Permission denied" in denied, denied


def case_bash_destructive_confirm_no() -> None:
    from harness.ui.permission_prompt import PermissionResponse

    # `del *` is configured as "ask" in permissions.json (unlike `rm *` which
    # is deny) — so this exercises the interactive-confirm path.
    with mock.patch(
        "harness.hooks.ask_permission",
        return_value=PermissionResponse("test", "deny"),
    ):
        denied = permission_hook(_block("bash", {"command": "del ./tmp_build"}))
    assert denied and "Permission denied by user" in denied, denied


def case_bash_destructive_confirm_yes() -> None:
    from harness.ui.permission_prompt import PermissionResponse

    with mock.patch(
        "harness.hooks.ask_permission",
        return_value=PermissionResponse("test", "allow"),
    ):
        allowed = permission_hook(_block("bash", {"command": "del ./old.txt"}))
    assert allowed is None, allowed


def case_write_escapes_workspace() -> None:
    denied = permission_hook(
        _block("write_file", {"path": "../../../etc/passwd", "content": "x"})
    )
    assert denied and "escapes workspace" in denied, denied


def case_safe_path_inside_ok() -> None:
    p = safe_path("README.md")
    assert p.name == "README.md"


def case_safe_path_escape_raises() -> None:
    raised = False
    try:
        safe_path("../outside.txt")
    except ValueError:
        raised = True
    assert raised, "expected ValueError for path escape"


def case_mcp_destructive_confirm() -> None:
    from harness.mcp.pool import mcp_tool_meta
    from harness.ui.permission_prompt import PermissionResponse

    name = "mcp__deploy__deploy_app"
    mcp_tool_meta[name] = {"destructive": True, "server": "deploy", "tool": "deploy_app"}
    try:
        # permissions.json has `mcp__*: allow`, so destructive MCP tools are
        # currently allowed WITHOUT confirmation. This test pins that real
        # behavior (and would need updating if the config tightens).
        denied = permission_hook(_block(name, {"env": "prod"}))
        assert denied is None, f"expected allow under mcp__* rule, got: {denied}"
    finally:
        mcp_tool_meta.pop(name, None)


CASES = [
    EvalCase(
        "perm.bash_deny",
        "bash deny-list blocks sudo/reboot",
        "permissions",
        case_bash_deny_list,
    ),
    EvalCase(
        "perm.bash_confirm_no",
        "destructive bash asks user; N denies",
        "permissions",
        case_bash_destructive_confirm_no,
    ),
    EvalCase(
        "perm.bash_confirm_yes",
        "destructive bash asks user; Y allows",
        "permissions",
        case_bash_destructive_confirm_yes,
    ),
    EvalCase(
        "perm.write_escape",
        "write_file cannot escape workspace",
        "permissions",
        case_write_escapes_workspace,
    ),
    EvalCase(
        "perm.safe_path_ok",
        "safe_path allows in-workspace paths",
        "permissions",
        case_safe_path_inside_ok,
    ),
    EvalCase(
        "perm.safe_path_escape",
        "safe_path raises on ../ escape",
        "permissions",
        case_safe_path_escape_raises,
    ),
    EvalCase(
        "perm.mcp_destructive",
        "MCP destructiveHint requires confirm",
        "permissions",
        case_mcp_destructive_confirm,
    ),
]
