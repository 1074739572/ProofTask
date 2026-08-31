"""Focused contract tests for the non-compatible Goal planning v2 format."""

import json

import pytest

from harness.goal.planner import (
    GoalPlanningError,
    _implementation_candidates,
    _parse_plan_result,
    build_plan_prompt,
    build_plan_review_prompt,
    parse_plan,
    plan_tasks,
)


def test_reviewer_approved_with_low_findings_is_warning(monkeypatch):
    from harness.goal.planner import _review_result

    approved, result, error = _review_result(
        '{"approved":true,"summary":"ready","findings":[{"severity":"low","task":"task","issue":"minor note","repair":"record warning"}]}'
    )

    assert error is None
    assert approved is True
    assert result["findings"][0]["severity"] == "low"


def test_reviewer_approved_with_blocking_findings_becomes_rejection():
    from harness.goal.planner import _review_result

    approved, result, error = _review_result(
        '{"approved":true,"summary":"ready","findings":[{"severity":"medium","task":"task","issue":"missing case","repair":"add case"}]}'
    )

    assert error is None
    assert approved is False
    assert result["approved"] is False
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


def test_v2_planner_continues_truncated_json_even_without_max_token_stop_reason():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '{"goal_contract":{"summary":"cut off'
        return json.dumps(_plan())

    result = plan_tasks(
        "add a rate limit", "pytest -q", planner_runner=planner,
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(),
    )

    assert result.tasks
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 24_000


def test_execution_replan_repair_uses_compact_prompt():
    original = _plan()
    candidate = parse_plan(json.dumps(original), discovery_manifest=MANIFEST, test_catalog=TestCatalog())
    assert candidate is not None
    scope = ({
        "task_id": "task_old", "name": "finish transcript", "behavior": "render every log entry",
        "acceptance_cases": [{"id": "AC1", "given": "logs", "when": "shown", "then": "all render"}],
    },)
    calls = []
    repaired = {"tasks": original["tasks"], "replacement_coverage": [{
        "superseded_task_id": "task_old", "acceptance_case_ids": ["AC1"],
        "replacement_task_names": ["enforce rate limit"],
    }]}
    result = plan_tasks(
        "fix log view", "pytest -q", planner_runner=lambda **kwargs: calls.append(kwargs) or json.dumps(repaired),
        reviewer_runner=lambda **_: '{"approved":true,"summary":"ready","findings":[]}',
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(), frozen_contract=original["goal_contract"],
        replacement_scope=scope, candidate_plan=candidate,
        review_feedback={"approved": False, "summary": "missing scope", "findings": [
            {"severity": "high", "task": "finish transcript", "issue": "missing", "repair": "cover it"},
        ]},
    )
    assert result.tasks
    assert "repo_files" not in calls[0]["prompt"]
    assert "Superseded behavior closure" in calls[0]["prompt"]


def test_execution_replan_repair_continues_truncated_response():
    original = _plan()
    candidate = parse_plan(json.dumps(original), discovery_manifest=MANIFEST, test_catalog=TestCatalog())
    assert candidate is not None
    scope = ({
        "task_id": "task_old", "name": "finish transcript", "behavior": "render every log entry",
        "acceptance_cases": [{"id": "AC1", "given": "logs", "when": "shown", "then": "all render"}],
    },)
    repaired = {"tasks": original["tasks"], "replacement_coverage": [{
        "superseded_task_id": "task_old", "acceptance_case_ids": ["AC1"],
        "replacement_task_names": ["enforce rate limit"],
    }]}
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        return '{"tasks":[' if len(calls) == 1 else json.dumps(repaired)

    result = plan_tasks(
        "fix log view", "pytest -q", planner_runner=planner,
        reviewer_runner=lambda **_: '{"approved":true,"summary":"ready","findings":[]}',
        discovery_manifest=MANIFEST, test_catalog=TestCatalog(), frozen_contract=original["goal_contract"],
        replacement_scope=scope, candidate_plan=candidate,
        review_feedback={"approved": False, "summary": "missing scope", "findings": [
            {"severity": "high", "task": "finish transcript", "issue": "missing", "repair": "cover it"},
        ]},
    )
    assert result.tasks
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 24_000


def test_valid_candidate_can_resume_at_review_without_replanning():
    checkpoints = []

    with pytest.raises(GoalPlanningError, match="reviewer returned invalid JSON"):
        plan_tasks(
            "add a rate limit", "pytest -q",
            planner_runner=lambda **_: json.dumps(_plan()),
            reviewer_runner=lambda **_: "",
            discovery_manifest=MANIFEST,
            test_catalog=TestCatalog(),
            candidate_callback=checkpoints.append,
        )

    assert len(checkpoints) == 1
    review_calls = []
    resumed = plan_tasks(
        "add a rate limit", "pytest -q",
        planner_runner=lambda **_: pytest.fail("a saved candidate must not call the planner again"),
        reviewer_runner=lambda **kwargs: review_calls.append(kwargs["agent_type"]) or '{"approved":true,"summary":"ready","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        candidate_plan=checkpoints[0],
    )

    assert resumed.review["approved"] is True
    assert review_calls == ["goal_plan_reviewer"]


def test_saved_replan_candidate_with_reviewer_findings_retries_only_the_correction():
    original = _plan()
    candidate = parse_plan(json.dumps(original), discovery_manifest=MANIFEST, test_catalog=TestCatalog())
    assert candidate is not None
    planner_calls = []
    reviewer_calls = []

    result = plan_tasks(
        "add a rate limit", "pytest -q",
        planner_runner=lambda **kwargs: planner_calls.append(kwargs["description"]) or json.dumps({"tasks": original["tasks"]}),
        reviewer_runner=lambda **kwargs: reviewer_calls.append(kwargs["agent_type"]) or '{"approved":true,"summary":"corrected","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        frozen_contract=original["goal_contract"],
        candidate_plan=candidate,
        review_feedback={
            "approved": False,
            "summary": "Task scope needs a correction.",
            "findings": [{"severity": "high", "task": "enforce rate limit", "issue": "scope", "repair": "narrow it"}],
        },
    )

    assert result.review["approved"] is True
    assert planner_calls == ["repair GoalPlan v2 after independent review"]
    assert reviewer_calls == ["goal_plan_reviewer"]


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


def test_execution_replan_injects_the_frozen_contract_instead_of_parsing_model_copy():
    original = _plan()
    changed = _plan()
    changed["goal_contract"]["summary"] = "A different product outcome."
    changed.pop("goal_contract")

    result = plan_tasks(
        "add a rate limit", "pytest -q",
        planner_runner=lambda **_: json.dumps(changed),
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        frozen_contract=original["goal_contract"],
    )

    assert result.contract == original["goal_contract"]


def test_execution_replan_prompt_requests_tasks_without_a_model_contract_copy():
    prompt = build_plan_prompt(
        "add a rate limit", "pytest -q", test_catalog=TestCatalog(),
        frozen_contract=_plan()["goal_contract"],
    )

    assert "will be injected by the system" in prompt
    assert "Do not include goal_contract in your response" in prompt
    assert 'Reply with ONLY one JSON object in this schema:\n{"tasks":[' in prompt


def test_execution_replan_prompts_include_the_superseded_task_closure():
    scope = ({
        "task_id": "task_old",
        "name": "finish transcript",
        "behavior": "render every log entry",
        "acceptance_cases": [{"id": "AC1", "given": "logs", "when": "shown", "then": "all render"}],
        "primary_write": ["src/App.tsx"],
        "planned_new": [], "conditional_write": [], "read_envelope": ["src/App.tsx"],
        "verification": {"source": "needs_generation"},
    },)
    prompt = build_plan_prompt(
        "fix log view", "pytest -q", test_catalog=TestCatalog(),
        frozen_contract=_plan()["goal_contract"], replacement_scope=scope,
    )
    review = build_plan_review_prompt(
        parse_plan(json.dumps(_plan()), discovery_manifest=MANIFEST, test_catalog=TestCatalog()),
        completed_task_names=("retained setup",), replacement_scope=scope,
    )

    assert "exact replacement obligation" in prompt
    assert "replacement_coverage" in prompt
    assert "finish transcript" in prompt
    assert "not against work owned by retained Tasks" in review
    assert "render every log entry" in review


def test_execution_replan_repairs_a_saved_candidate_missing_closure_coverage():
    original = _plan()
    candidate = parse_plan(json.dumps(original), discovery_manifest=MANIFEST, test_catalog=TestCatalog())
    assert candidate is not None
    scope = ({
        "task_id": "task_old",
        "name": "finish transcript",
        "behavior": "render every log entry",
        "acceptance_cases": [{"id": "AC1", "given": "logs", "when": "shown", "then": "all render"}],
    },)
    repaired = {"tasks": original["tasks"], "replacement_coverage": [{
        "superseded_task_id": "task_old",
        "acceptance_case_ids": ["AC1"],
        "replacement_task_names": ["enforce rate limit"],
    }]}
    calls = []
    reviewer_calls = []

    result = plan_tasks(
        "fix log view", "pytest -q",
        planner_runner=lambda **kwargs: calls.append(kwargs["description"]) or json.dumps(repaired),
        reviewer_runner=lambda **kwargs: reviewer_calls.append(kwargs["agent_type"]) or '{"approved":true,"summary":"ready","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        frozen_contract=original["goal_contract"],
        replacement_scope=scope,
        candidate_plan=candidate,
        review_feedback={
            "approved": False,
            "summary": "The saved candidate omitted scope.",
            "findings": [{"severity": "high", "task": "finish transcript", "issue": "missing", "repair": "cover it"}],
        },
    )

    assert result.review["approved"] is True
    assert calls == ["repair GoalPlan v2 JSON"]
    assert reviewer_calls == ["goal_plan_reviewer"]


def test_execution_replan_normalizes_scope_labels_against_current_worktree():
    raw = _plan(primary_write=["src/rate_limit.py"], planned_new=["src/app.py"])
    plan = parse_plan(
        json.dumps({**raw, "tasks": [{**raw["tasks"][0], "primary_write": ["src/rate_limit.py"], "planned_new": ["src/app.py"]}]}),
        discovery_manifest={**MANIFEST, "repo_files": [*MANIFEST["repo_files"], "src/rate_limit.py"], "evidence": [*MANIFEST["evidence"], {"id": "E3", "path": "src/rate_limit.py", "claim": "existing module"}]},
        test_catalog=TestCatalog(),
    )
    assert plan is None  # Initial planning keeps strict scope validation.
    plan, error = _parse_plan_result(
        json.dumps({**raw, "tasks": [{**raw["tasks"][0], "primary_write": ["src/rate_limit.py"], "planned_new": ["src/app.py"]}]}),
        discovery_manifest={**MANIFEST, "repo_files": [*MANIFEST["repo_files"], "src/rate_limit.py"], "evidence": [*MANIFEST["evidence"], {"id": "E3", "path": "src/rate_limit.py", "claim": "existing module"}]},
        test_catalog=TestCatalog(), contract_override=raw["goal_contract"],
    )
    assert error is None
    assert plan is not None
    assert plan.tasks[0].primary_write == ("src/rate_limit.py", "src/app.py")
    assert plan.tasks[0].planned_new == ()


def test_execution_replan_reviewer_receives_worker_created_file_takeover_context():
    raw = _plan(primary_write=["src/GoalSummary.tsx"], planned_new=[])
    raw["tasks"][0]["read_envelope"] = ["src/app.py"]
    raw["replacement_coverage"] = [{
        "superseded_task_id": "task_old",
        "acceptance_case_ids": ["AC1"],
        "replacement_task_names": ["enforce rate limit"],
    }]
    scope = ({
        "task_id": "task_old",
        "name": "old summary work",
        "behavior": "create and render a summary component",
        "acceptance_cases": raw["tasks"][0]["acceptance_cases"],
        "planned_new": ["src/GoalSummary.tsx"],
    },)
    execution_manifest = {
        **MANIFEST,
        "repo_files": [*MANIFEST["repo_files"], "src/GoalSummary.tsx"],
        "evidence": [*MANIFEST["evidence"], {
            "id": "EXECUTION_SNAPSHOT_1",
            "path": "src/GoalSummary.tsx",
            "claim": "Existing source file in the current Goal execution worktree.",
        }],
    }
    reviewer_prompts = []

    result = plan_tasks(
        "repair summary", "pytest -q",
        planner_runner=lambda **_: json.dumps(raw),
        reviewer_runner=lambda **kwargs: reviewer_prompts.append(kwargs["prompt"]) or '{"approved":true,"summary":"ready","findings":[]}',
        discovery_manifest=execution_manifest,
        test_catalog=TestCatalog(),
        frozen_contract=raw["goal_contract"],
        replacement_scope=scope,
        execution_workspace_paths=("src/GoalSummary.tsx",),
    )

    assert result.review["approved"] is True
    assert result.tasks[0].primary_write == ("src/GoalSummary.tsx",)
    assert result.tasks[0].planned_new == ()
    assert "src/GoalSummary.tsx" in reviewer_prompts[0]
    assert "must not be required to repeat its creation in planned_new" in reviewer_prompts[0]


def test_execution_replan_accepts_new_file_under_an_empty_existing_directory():
    raw = _plan(primary_write=[], planned_new=["src/generated/view.ts"])
    manifest = {
        **MANIFEST,
        "repo_files": [*MANIFEST["repo_files"]],
        "repo_dirs": ["src", "src/generated"],
        "evidence": [*MANIFEST["evidence"]],
    }
    initial = parse_plan(json.dumps(raw), discovery_manifest=manifest, test_catalog=TestCatalog())
    assert initial is None  # Initial manifests do not trust an un-evidenced empty directory.
    plan, error = _parse_plan_result(
        json.dumps(raw),
        discovery_manifest=manifest,
        test_catalog=TestCatalog(),
        contract_override=raw["goal_contract"],
    )
    assert error is None
    assert plan is not None
    assert plan.tasks[0].planned_new == ("src/generated/view.ts",)


def test_new_goal_can_supersede_but_never_migrate_an_old_goal(tmp_path):
    path = goal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 3, "id": "goal_old", "phase": "paused"}), encoding="utf-8")

    archived = archive_unsupported_goal(tmp_path)

    assert archived.exists()
    assert json.loads(archived.read_text(encoding="utf-8"))["schema_version"] == 3
    assert not path.exists()
