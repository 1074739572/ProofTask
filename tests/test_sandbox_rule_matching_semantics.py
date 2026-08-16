"""Lock the sandbox rule matching semantics.

The sandbox deny-list relies on these semantics and a refactor must preserve
them:

* the last matching bash rule wins, so a deny is never undone by an earlier
  allow;
* specific MCP rules beat wildcard MCP rules regardless of order;
* tool name wildcards cover MCP tools;
* MCP annotations only act as a fallback when no config rule matches.
"""

from __future__ import annotations

from harness.permissions.engine import evaluate_permission


def test_bash_deny_rule_is_not_undone_by_an_earlier_allow():
    rules = {
        "bash": {
            "git *": "allow",
            "git push*": "deny",
        }
    }
    assert evaluate_permission(
        "bash", {"command": "git push origin main"}, rules=rules, include_saved=False
    ).effect == "deny"
    assert evaluate_permission(
        "bash", {"command": "git status"}, rules=rules, include_saved=False
    ).effect == "allow"


def test_specific_mcp_rule_beats_wildcard_rule_in_any_order():
    tool = "mcp__github__delete_branch"
    for rules in (
        {"mcp__github__delete_branch": "ask", "mcp__*": "allow"},
        {"mcp__*": "allow", "mcp__github__delete_branch": "ask"},
    ):
        assert evaluate_permission(
            tool, {"branch": "main"}, rules=rules, include_saved=False
        ).effect == "ask"


def test_tool_name_wildcards_cover_mcp_tools():
    rules = {
        "*": "allow",
        "mcp__*": "ask",
        "mcp__github__delete_branch": "deny",
    }
    assert evaluate_permission(
        "mcp__fetch__fetch", {"url": "https://example.com"}, rules=rules, include_saved=False
    ).effect == "ask"
    assert evaluate_permission(
        "mcp__github__delete_branch", {"branch": "main"}, rules=rules, include_saved=False
    ).effect == "deny"


def test_mcp_annotations_are_only_a_fallback_when_no_config_rule_matches():
    # No config rules: a readOnly annotation allows, an unknown tool asks.
    decision = evaluate_permission(
        "mcp__docs__search",
        {"query": "permissions"},
        mcp_meta={"readOnly": True},
        rules={},
        include_saved=False,
    )
    assert decision.effect == "allow"
    assert decision.source == "mcp"

    decision = evaluate_permission(
        "mcp__unknown__mutate",
        {"id": "1"},
        mcp_meta={},
        rules={},
        include_saved=False,
    )
    assert decision.effect == "ask"
    assert decision.source == "mcp"


def test_config_rule_wins_over_mcp_annotations():
    # A matching config rule takes precedence; the annotation is only a
    # fallback, so a deny/ask config rule is not overridden by readOnly.
    rules = {"mcp__docs__search": "deny"}
    assert evaluate_permission(
        "mcp__docs__search",
        {"query": "permissions"},
        mcp_meta={"readOnly": True},
        rules=rules,
        include_saved=False,
    ).effect == "deny"

    rules = {"mcp__docs__search": "ask"}
    assert evaluate_permission(
        "mcp__docs__search",
        {"query": "permissions"},
        mcp_meta={"readOnly": True},
        rules=rules,
        include_saved=False,
    ).effect == "ask"
