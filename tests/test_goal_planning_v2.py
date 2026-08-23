"""Focused contract tests for the non-compatible Goal planning v2 format."""

import json

import pytest

from harness.goal.planner import GoalPlanningError, parse_plan, plan_tasks
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
