"""Focused contract tests for the non-compatible Goal planning v2 format."""

import json

import pytest

from harness.goal.planner import GoalPlanningError, _implementation_candidates, build_plan_prompt, build_plan_review_prompt, parse_plan, plan_tasks
from harness.goal.store import archive_unsupported_goal, goal_path
from harness.verification.catalog import TestCatalog


MANIFEST = {
    "repo_files": ["src/app.py", "tests/test_app.py", "docs/requirements.md"],
    "evidence": [
        {"id": "E1", "path": "src/app.py", "claim": "existing application entry"},
        {"id": "E2", "path": "docs/requirements.md", "claim": "requested behavior"},
    ],
}


def _plan(*, primary_write=None, planned_new=None):
    return {
        "goal_contract": {
            "summary": "Add an observable rate limit.",
            "constraints": ["Keep existing API compatibility."],
            "assumptions": [],
            "unresolved": [],
            "verification_preconditions": [],
            "decision_ledger": [{
                "id": "D1", "decision": "Keep the limit in the application entry.",
                "rationale": "The entry owns request handling.", "evidence_refs": ["E1"],
            }],
        },
        "tasks": [{
            "name": "enforce rate limit",
            "behavior": "Reject requests after the configured limit.",
            "acceptance_cases": [{"id": "AC1", "given": "a caller used the limit", "when": "another request arrives", "then": "the request is rejected"}],
            "depends_on": [],
            "primary_write": primary_write if primary_write is not None else ["src/app.py"],
            "planned_new": planned_new if planned_new is not None else [],
            "conditional_write": [],
            "read_envelope": ["src", "tests"],
            "forbidden": [".env"],
            "evidence_refs": ["E1", "E2"],
            "test_strategy": "Generate a focused request-limit regression test.",
            "test_selectors": [],
        }],
    }


def test_v2_plan_compiles_scope_classes_and_independent_review():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs["agent_type"])
        return json.dumps(_plan())

    def reviewer(**kwargs):
        calls.append(kwargs["agent_type"])
        return '{"approved":true,"summary":"executable","findings":[]}'

    result = plan_tasks(
        "add a rate limit", "pytest -q", planner_runner=planner, reviewer_runner=reviewer,
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    )

    assert calls == ["goal_planner", "goal_plan_reviewer"]
    assert result.contract["decision_ledger"][0]["id"] == "D1"
    assert result.tasks[0].primary_write == ("src/app.py",)
    assert result.tasks[0].planned_new == ()
    assert result.review["approved"] is True


def test_v2_plan_streams_the_planner_response():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        return json.dumps(_plan())

    result = plan_tasks(
        "add a rate limit", "pytest -q", planner_runner=planner,
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    )

    assert result.tasks
    assert calls[0]["stream_response"] is True
    assert calls[0]["request_read_timeout_seconds"] == 300.0
    assert calls[0]["max_request_attempts"] == 1


def test_v2_planner_continues_once_with_an_upgraded_output_budget():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["stats"].stop_reason = "max_tokens"
            return '{"goal_contract":'
        return json.dumps(_plan())

    result = plan_tasks(
        "add a rate limit", "pytest -q", planner_runner=planner,
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    )

    assert result.tasks
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 12_000
    assert calls[1]["max_tokens"] == 24_000
    assert calls[0]["conversation"] is calls[1]["conversation"]
    assert "exhausted its output budget" in calls[1]["prompt"]


def test_v2_allows_a_new_module_only_under_an_evidenced_parent():
    result = parse_plan(
        json.dumps(_plan(primary_write=[], planned_new=["src/rate_limit.py"])),
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
    )

    assert result is not None
    assert result.tasks[0].planned_new == ("src/rate_limit.py",)


def test_v2_rejects_removed_scope_paths():
    raw = _plan()
    raw["tasks"][0]["scope_paths"] = ["src"]

    assert parse_plan(json.dumps(raw), discovery_manifest=MANIFEST, test_catalog=TestCatalog()) is None


def test_unknown_read_envelope_requests_discovery_refresh():
    raw = _plan()
    raw["tasks"][0]["read_envelope"] = ["src/missing.py"]
    responses = iter((json.dumps(raw), json.dumps(raw)))

    with pytest.raises(GoalPlanningError) as exc_info:
        plan_tasks(
            "add a rate limit",
            "pytest -q",
            planner_runner=lambda **_: next(responses),
            discovery_manifest=MANIFEST,
            test_catalog=TestCatalog(),
        )

    assert exc_info.value.requires_discovery_refresh is True
    assert "src/missing.py" in str(exc_info.value)


def test_planned_new_file_is_not_retained_in_read_envelope():
    raw = _plan(primary_write=[], planned_new=["src/rate_limit.py"])
    raw["tasks"][0]["read_envelope"] = ["src", "src/rate_limit.py"]

    plan = parse_plan(json.dumps(raw), discovery_manifest=MANIFEST, test_catalog=TestCatalog())

    assert plan is not None
    assert plan.tasks[0].planned_new == ("src/rate_limit.py",)
    assert plan.tasks[0].read_envelope == ("src",)


def test_later_dependent_task_can_read_an_earlier_planned_new_file():
    raw = _plan(primary_write=[], planned_new=["src/rate_limit.py"])
    raw["tasks"].append({
        **_plan()["tasks"][0],
        "name": "wire rate limit",
        "depends_on": ["enforce rate limit"],
        "read_envelope": ["src/app.py", "src/rate_limit.py"],
    })

    plan = parse_plan(json.dumps(raw), discovery_manifest=MANIFEST, test_catalog=TestCatalog())

    assert plan is not None
    assert plan.tasks[1].read_envelope == ("src/app.py", "src/rate_limit.py")


def test_reviewer_rejection_gets_one_planner_correction():
    planner_responses = iter([json.dumps(_plan()), json.dumps(_plan(planned_new=["src/rate_limit.py"]))])
    review_responses = iter([
        '{"approved":false,"summary":"new module is clearer","findings":[{"severity":"medium","task":"enforce rate limit","issue":"implementation mixes policy into entry","repair":"plan a dedicated module"}]}',
        '{"approved":true,"summary":"executable","findings":[]}',
    ])

    result = plan_tasks(
        "add a rate limit", "pytest -q", planner_runner=lambda **_: next(planner_responses),
        reviewer_runner=lambda **_: next(review_responses), discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    )

    assert result.tasks[0].planned_new == ("src/rate_limit.py",)


def test_reviewer_cannot_block_execution_with_invalid_json():
    with pytest.raises(GoalPlanningError, match="reviewer returned invalid JSON"):
        plan_tasks(
            "add a rate limit", "pytest -q", planner_runner=lambda **_: json.dumps(_plan()),
            reviewer_runner=lambda **_: "looks fine", discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
        )


def test_planning_contract_distinguishes_static_config_from_session_state():
    prompt = build_plan_prompt(
        "add a current-session permission command without persisting its selected mode",
        "pytest -q",
        discovery_manifest=MANIFEST,
    )
    review = build_plan_review_prompt(parse_plan(
        json.dumps(_plan()), discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    ))

    assert "persistent implementation artifacts from runtime user selections" in prompt
    assert "static schema/default support while the runtime selection remains session-scoped" in prompt
    assert "checked-in configuration file may be primary_write" in review
    assert "writes that runtime choice back to the configuration" in review
    assert "command/input registration, current-session state, and centralized enforcement hook" in review


def test_planning_can_scope_machine_indexed_entry_points_named_by_discovery():
    manifest = {
        "repo_files": ["harness/cli.py", "harness/hooks.py", "docs/architecture.md"],
        "evidence": [{
            "id": "E1",
            "path": "docs/architecture.md",
            "claim": "Register /permission in cli.py and enforce it through the PreToolUse hook in hooks.py.",
        }],
    }
    raw = _plan(primary_write=["harness/cli.py", "harness/hooks.py"])
    raw["tasks"][0]["read_envelope"] = ["harness", "docs"]
    raw["tasks"][0]["evidence_refs"] = ["E1"]

    plan = parse_plan(json.dumps(raw), discovery_manifest=manifest, test_catalog=TestCatalog())

    assert _implementation_candidates(manifest) == ("harness/cli.py", "harness/hooks.py")
    assert plan is not None
    assert plan.tasks[0].primary_write == ("harness/cli.py", "harness/hooks.py")


def test_planning_can_scope_dotted_python_modules_named_by_discovery():
    manifest = {
        "repo_files": ["harness/agents/runner.py", "docs/architecture.md"],
        "evidence": [{
            "id": "E1",
            "path": "docs/architecture.md",
            "claim": "Route Goal worker commands through harness.agents.runner.",
        }],
    }
    raw = _plan(primary_write=["harness/agents/runner.py"])
    raw["tasks"][0]["read_envelope"] = ["harness/agents"]
    raw["tasks"][0]["evidence_refs"] = ["E1"]

    plan = parse_plan(json.dumps(raw), discovery_manifest=manifest, test_catalog=TestCatalog())

    assert _implementation_candidates(manifest) == ("harness/agents/runner.py",)
    assert plan is not None


def test_execution_replan_may_depend_on_completed_task_but_cannot_change_contract():
    original = _plan()
    replacement = _plan()
    replacement["tasks"][0]["depends_on"] = ["prepare storage"]

    result = plan_tasks(
        "add a rate limit", "pytest -q",
        planner_runner=lambda **_: json.dumps(replacement),
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        frozen_contract=original["goal_contract"],
        completed_task_names=("prepare storage",),
    )

    assert result.tasks[0].depends_on == ("prepare storage",)


def test_execution_replan_rejects_an_attempt_to_change_the_frozen_contract():
    original = _plan()
    changed = _plan()
    changed["goal_contract"]["summary"] = "A different product outcome."

    with pytest.raises(GoalPlanningError, match="frozen goal_contract"):
        plan_tasks(
            "add a rate limit", "pytest -q",
            planner_runner=lambda **_: json.dumps(changed),
            reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
            discovery_manifest=MANIFEST,
            test_catalog=TestCatalog(),
            frozen_contract=original["goal_contract"],
        )


def test_new_goal_can_supersede_but_never_migrate_an_old_goal(tmp_path):
    path = goal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 3, "id": "goal_old", "phase": "paused"}), encoding="utf-8")

    archived = archive_unsupported_goal(tmp_path)

    assert archived.exists()
    assert json.loads(archived.read_text(encoding="utf-8"))["schema_version"] == 3
    assert not path.exists()
