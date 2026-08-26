"""静态权限策略与风险分级模型的回归测试。

``harness/permission_policy.py`` 是纯解析与分级模块：加载并校验扩展后的
``config/permissions.json`` 静态 schema，暴露 default / auto-review /
full-access 三个模式的自动放行风险等级，并把工具调用分级为
low / medium / high / blocked。本任务只实现静态策略与处理逻辑，不读取或
修改运行时模式。

该模块目前尚未实现，因此所有测试通过 ``_policy_api()`` 延迟导入：
pytest 可以在实现前收集本文件，运行时会因缺失行为（ImportError）失败。
"""

from __future__ import annotations

import json

import pytest

from harness.settings import BUILTIN_CONFIG_DIR

SHIPPED_PERMISSIONS_PATH = BUILTIN_CONFIG_DIR / "permissions.json"
SHIPPED_MODES_PATH = BUILTIN_CONFIG_DIR / "modes.json"

EXPECTED_MODES = ("default", "auto-review", "full-access")
RISK_LEVELS = {"low", "medium", "high", "blocked"}


def _policy_api():
    """延迟导入计划中的模块，保证实现前本文件仍可被 pytest 收集。"""
    from harness.permission_policy import (
        PermissionPolicyConfigError,
        classify_tool,
        load_permission_policy,
    )

    return load_permission_policy, classify_tool, PermissionPolicyConfigError


# --- AC1：静态 schema 与三模式自动放行等级 ---


def test_ac1_shipped_config_exposes_all_three_modes():
    load_permission_policy, _, _ = _policy_api()

    policy = load_permission_policy(SHIPPED_PERMISSIONS_PATH)

    assert set(policy.modes) == set(EXPECTED_MODES)


def test_ac1_mode_auto_approve_levels():
    load_permission_policy, _, _ = _policy_api()

    policy = load_permission_policy(SHIPPED_PERMISSIONS_PATH)

    assert set(policy.auto_approve_levels("default")) == {"low"}
    assert set(policy.auto_approve_levels("auto-review")) == {"low", "medium"}
    assert set(policy.auto_approve_levels("full-access")) == {"low", "medium", "high"}


def test_ac1_blocked_is_never_auto_approved_in_any_mode():
    load_permission_policy, _, _ = _policy_api()

    policy = load_permission_policy(SHIPPED_PERMISSIONS_PATH)

    for mode in EXPECTED_MODES:
        levels = set(policy.auto_approve_levels(mode))
        assert "blocked" not in levels, mode
        assert levels <= {"low", "medium", "high"}, mode


# --- AC2：工具名分类 ---


def test_ac2_read_only_tools_classify_low():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("read_file", {"path": "README.md"}) == "low"
    assert classify_tool("rag_search", {"query": "permissions"}) == "low"


def test_ac2_file_write_and_edit_tools_classify_medium():
    _, classify_tool, _ = _policy_api()

    assert (
        classify_tool("write_file", {"path": "notes.txt", "content": "x"}) == "medium"
    )
    assert (
        classify_tool(
            "edit_file",
            {"path": "notes.txt", "old_text": "a", "new_text": "b"},
        )
        == "medium"
    )


def test_ac2_unrecognized_tool_conservatively_classifies_high():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("totally_unknown_tool", {}) == "high"
    assert classify_tool("mcp__madeup__mutate", {"id": "1"}) == "high"


def test_ac2_subagent_classifies_by_content():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("subagent", {"prompt": "summarize the repo"}) == "high"


def test_ac2_bash_hard_blocked_command_is_blocked():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("bash", {"command": "sudo rm -rf /"}) == "blocked"


# --- AC3：bash 命令内容联合分级 ---


def test_ac3_bash_non_destructive_query_commands_classify_low():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("bash", {"command": "ls -la"}) == "low"
    assert classify_tool("bash", {"command": "git status"}) == "low"


def test_ac3_bash_destructive_command_classifies_high():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("bash", {"command": "rm -rf /tmp/x"}) == "high"


def test_ac3_bash_unknown_command_conservatively_classifies_high():
    _, classify_tool, _ = _policy_api()

    assert classify_tool("bash", {"command": "frobnicate --all-the-things"}) == "high"


def test_ac3_classify_tool_only_returns_defined_risk_levels():
    _, classify_tool, _ = _policy_api()

    samples = [
        ("read_file", {"path": "a.txt"}),
        ("write_file", {"path": "a.txt"}),
        ("bash", {"command": "ls -la"}),
        ("bash", {"command": "rm -rf /tmp/x"}),
        ("subagent", {"prompt": "hi"}),
        ("unknown_tool", {}),
    ]
    for tool, tool_input in samples:
        assert classify_tool(tool, tool_input) in RISK_LEVELS


# --- AC4：配置校验失败必须显式报错，不允许静默产生未定义权限策略 ---


def _write_config(tmp_path, payload: str):
    config_path = tmp_path / "permissions.json"
    config_path.write_text(payload, encoding="utf-8")
    return config_path


def test_ac4_missing_modes_field_raises_config_error(tmp_path):
    load_permission_policy, _, PermissionPolicyConfigError = _policy_api()

    config_path = _write_config(tmp_path, json.dumps({"tools": {}}))

    with pytest.raises((PermissionPolicyConfigError, ValueError)):
        load_permission_policy(config_path)


def test_ac4_mode_missing_auto_approve_field_raises_config_error(tmp_path):
    load_permission_policy, _, PermissionPolicyConfigError = _policy_api()

    config_path = _write_config(
        tmp_path,
        json.dumps({"modes": {"default": {"risk_rules": {}}}}),
    )

    with pytest.raises((PermissionPolicyConfigError, ValueError)):
        load_permission_policy(config_path)


def test_ac4_illegal_mode_auto_approve_level_raises_config_error(tmp_path):
    load_permission_policy, _, PermissionPolicyConfigError = _policy_api()

    payload = {
        "modes": {
            "default": {"auto_approve": ["banana"]},
            "auto-review": {"auto_approve": ["low", "medium"]},
            "full-access": {"auto_approve": ["low", "medium", "high"]},
        }
    }
    config_path = _write_config(tmp_path, json.dumps(payload))

    with pytest.raises((PermissionPolicyConfigError, ValueError)):
        load_permission_policy(config_path)


def test_ac4_malformed_config_json_raises_config_error(tmp_path):
    load_permission_policy, _, PermissionPolicyConfigError = _policy_api()

    config_path = _write_config(tmp_path, "{not valid json")

    with pytest.raises((PermissionPolicyConfigError, ValueError)):
        load_permission_policy(config_path)


# --- AC5 / AC6：加载过程只读，不回写运行时状态 ---


def test_ac5_loading_config_never_rewrites_the_file(tmp_path):
    load_permission_policy, _, _ = _policy_api()

    config_path = tmp_path / "permissions.json"
    config_path.write_bytes(SHIPPED_PERMISSIONS_PATH.read_bytes())
    before = config_path.read_bytes()

    load_permission_policy(config_path)

    assert config_path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["permissions.json"]


def test_ac6_modes_json_restrictions_survive_policy_load_unchanged():
    load_permission_policy, _, _ = _policy_api()

    before = SHIPPED_MODES_PATH.read_bytes()

    load_permission_policy(SHIPPED_PERMISSIONS_PATH)

    assert SHIPPED_MODES_PATH.read_bytes() == before

    modes = json.loads(before.decode("utf-8"))["modes"]
    assert "write_file" in modes["plan"]["disable_tools"]
    assert modes["grill"]["confirm_before_execute"] is True
