"""Regression tests for the interactive approval and remember flow.

Pins the acceptance contract that the interactive permission pipeline keeps
working under the sandbox policy: an ask decision still prompts, a session
approval is remembered for the rest of the session, an "always" approval is
recorded through ``add_persistent_rule``, session rules override config asks,
protected env files stay denied while ``.env.example`` stays readable,
external directories can be allowed before the tool rule applies, and a
top-level string policy such as ``{"permission": "allow"}`` still normalizes
to the equivalent wildcard rules.
"""

from __future__ import annotations

import json
from unittest import mock

from harness.hooks import permission_hook
from harness.permissions.config import (
    SANDBOX_PERMISSIONS,
    _normalize_rules,
    load_permission_rules,
)
from harness.permissions.engine import evaluate_permission
from harness.permissions.state import (
    add_session_rule,
    clear_session_rules,
    session_rules,
)


def test_ac1_session_approval_allows_the_next_evaluation():
    # AC1: an ask decision approved once with remember_session must allow the
    # next evaluation of the same tool and resource from the session rule.
    clear_session_rules()
    block = {"name": "bash", "input": {"command": "npm test"}}
    response = mock.Mock(
        decision="session", allowed=True, remember_session=True, remember_always=False, value=""
    )
    try:
        with mock.patch("harness.hooks.evaluate_permission") as evaluate, mock.patch(
            "harness.hooks.ask_permission", return_value=response
        ), mock.patch("harness.hooks.audit_permission"):
            evaluate.return_value = mock.Mock(
                effect="ask",
                resource="npm test",
                reason="matched bash:*",
                source="config",
                save_tool="bash",
                save_resource="npm test*",
                external_resource=None,
            )
            assert permission_hook(block) is None
        # The hook recorded a session rule for this exact tool and resource.
        assert any(
            rule.tool == "bash" and rule.resource == "npm test*" and rule.effect == "allow"
            for rule in session_rules()
        )
        # The next evaluation of the same tool and resource is allowed from the
        # session rule (the config still asks for this command).
        decision = evaluate_permission("bash", {"command": "npm test"})
        assert decision.effect == "allow"
        assert decision.source == "session"
    finally:
        clear_session_rules()


def test_ac2_always_approval_records_a_persistent_rule():
    # AC2: an ask decision approved with remember_always must record the
    # approval by calling add_persistent_rule with the tool, resource, and
    # effect allow.
    response = mock.Mock(
        decision="always", allowed=True, remember_session=False, remember_always=True, value=""
    )
    block = {"name": "bash", "input": {"command": "npm test"}}
    with mock.patch("harness.hooks.evaluate_permission") as evaluate, mock.patch(
        "harness.hooks.ask_permission", return_value=response
    ), mock.patch("harness.hooks.audit_permission"), mock.patch(
        "harness.hooks.add_persistent_rule"
    ) as add_persistent:
        evaluate.return_value = mock.Mock(
            effect="ask",
            resource="npm test",
            reason="matched bash:*",
            source="config",
            save_tool="bash",
            save_resource="npm test*",
            external_resource=None,
        )
        assert permission_hook(block) is None
    add_persistent.assert_called_once_with("bash", "npm test*", "allow")


def test_ac3_session_rule_overrides_a_config_ask():
    # AC3: a session allow rule and a config ask for the same tool evaluate to
    # allow with source session.
    clear_session_rules()
    try:
        add_session_rule("bash", "npm test*", "allow")
        decision = evaluate_permission(
            "bash",
            {"command": "npm test -- --watch=false"},
            rules={"bash": {"*": "ask"}},
        )
        assert decision.effect == "allow"
        assert decision.source == "session"
    finally:
        clear_session_rules()


def test_ac4_env_files_stay_denied_while_example_stays_readable():
    # AC4: sandbox read_file rules deny env files but allow examples, so
    # .env and .env.local are denied while .env.example stays readable.
    rules = SANDBOX_PERMISSIONS
    effects = [
        evaluate_permission("read_file", {"path": path}, rules=rules, include_saved=False).effect
        for path in (".env", ".env.local", ".env.example")
    ]
    assert effects == ["deny", "deny", "allow"]


def test_ac5_external_directory_allowed_then_tool_rule_applies():
    # AC5: once external_directory is allowed for a specific outside
    # directory and read_file is allowed, reading a file in that directory is
    # allowed on the read_file tool rule.
    rules = {
        "external_directory": {"C:/outside/project*": "allow"},
        "read_file": "allow",
    }
    decision = evaluate_permission(
        "read_file",
        {"path": "C:/outside/project/notes.txt"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "allow"
    assert decision.tool == "read_file"


def test_ac6_top_level_string_policy_normalizes_to_wildcard_rules(tmp_path, monkeypatch):
    # AC6: a permissions config with a top-level string policy such as
    # {"permission": "allow"} normalizes to the equivalent {"*": "allow"}.
    config_path = tmp_path / "permissions.json"
    config_path.write_text(json.dumps({"permission": "allow"}), encoding="utf-8")
    monkeypatch.setattr(
        "harness.permissions.config.get_permissions_config_path",
        lambda: config_path,
    )
    assert load_permission_rules() == {"*": "allow"}
    assert _normalize_rules({"permission": "allow"}) == {"*": "allow"}
