"""Core tests for strict Goal task persistence and planning."""

import json

from harness.goal.commands import parse_goal_command, parse_goal_subcommand
from harness.goal.engine import GoalEngine, GoalTransitionError
from harness.goal.models import GOAL_SCHEMA_VERSION, GoalPhase, GoalState, GoalStatus, StopReason
import pytest

from harness.goal.planner import GoalPlanningError, build_plan_prompt, parse_plan, plan_tasks
from harness.verification.catalog import TestCatalog
from harness.goal.store import (
    GoalLeaseError,
    GoalStoreError,
    acquire_goal_lease,
    goal_path,
    load_goal,
    release_goal_lease,
    save_goal,
)


def test_parse_worker_limits_without_a_goal_lifetime_budget():
    result = parse_goal_command(
        '/goal --verify "pytest -q" --worker-rounds 5 --operation-timeout 600 -- fix bug'
    )
    assert result["limits"] == {
        "worker_round_limit": 5,
        "operation_timeout_seconds": 600,
    }


def test_goal_stop_is_a_resumable_pause_alias():
    assert parse_goal_subcommand("/goal stop") == "pause"
    assert parse_goal_command("/goal stop") == {"action": "pause"}


def test_goal_state_has_no_default_lifetime_budget():
    state = GoalState.new(target="long task", verification="pytest -q", workspace=".")
    assert state.worker_round_limit == 20
    assert state.operation_timeout_seconds == 1800
    assert not hasattr(state, "max_duration_seconds")
    assert not hasattr(state, "max_total_rounds")


def test_plan_tasks_rejects_an_invalid_planner_result():
    with pytest.raises(GoalPlanningError):
        plan_tasks("fix everything", "pytest -q", planner_runner=lambda **_: "")


def test_planner_is_a_single_tool_free_contract_call():
    seen = {}

    def planner(**kwargs):
        seen.update(kwargs)
        return (
            '[{"name":"limit requests","behavior":"each user is limited",'
            '"acceptance_cases":[{"id":"AC1","given":"a user exceeds the limit",'
            '"when":"a request arrives","then":"the request is rejected"}],'
            '"test_selectors":[],"depends_on":[]}]'
        )

    plans = plan_tasks("limit requests", "pytest -q", planner_runner=planner)

    assert len(plans) == 1
    assert seen["max_rounds"] == 1
    assert seen["max_tokens"] == 32_000
    assert seen["tools_override"] == ()


def test_planner_retries_once_with_the_contract_error_and_accepts_the_repair():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '[{"name":"limit requests","behavior":"each user is limited","depends_on":[]}]'
        return (
            '[{"name":"limit requests","behavior":"each user is limited",'
            '"acceptance_cases":[{"id":"AC1","given":"a user exceeds the limit",'
            '"when":"a request arrives","then":"the request is rejected"}],'
            '"test_selectors":[],"depends_on":[]}]'
        )

    plans = plan_tasks("limit requests", "pytest -q", planner_runner=planner)

    assert len(plans) == 1
    assert len(calls) == 2
    assert calls[1]["max_rounds"] == 1
    assert "needs 1-8 valid acceptance_cases" in calls[1]["prompt"]


def test_planner_repair_receives_all_contract_errors_at_once():
    calls = []

    def planner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                '[{"name":"one","behavior":"first",'
                '"acceptance_cases":[{"id":"AC1","given":"x","when":"y","then":"z"}],"depends_on":[]},'
                '{"name":"two","behavior":"second",'
                '"acceptance_cases":[{"id":"AC1","given":"x","when":"y","then":"z"}],"depends_on":[]}]'
            )
        return (
            '[{"name":"one","behavior":"first",'
            '"acceptance_cases":[{"id":"AC1","given":"x","when":"y","then":"z"}],'
            '"depends_on":[],"scope_paths":["src/app.ts"],"test_strategy":"focused test"}]'
        )

    manifest = {"repo_files": ["src/app.ts"], "evidence": [{"id": "E1", "path": "src/app.ts"}]}
    plans = plan_tasks("fix it", "pytest -q", planner_runner=planner, discovery_manifest=manifest)

    assert len(plans) == 1
    assert "Task 1 (one) is missing scope_paths" in calls[1]["prompt"]
    assert "Task 2 (two) is missing scope_paths" in calls[1]["prompt"]


def test_planner_does_not_cap_a_large_goal_at_eight_tasks():
    entries = []
    for index in range(20):
        entries.append({
            "name": f"task {index + 1}",
            "behavior": f"deliver behavior {index + 1}",
            "acceptance_cases": [{
                "id": "AC1",
                "given": "the preceding work is available",
                "when": f"behavior {index + 1} is used",
                "then": f"deliverable {index + 1} is observable",
            }],
            "test_selectors": [],
            "depends_on": [f"task {index}"] if index else [],
        })

    plans = parse_plan(json.dumps(entries))

    assert plans is not None
    assert len(plans) == 20
    assert plans[-1].depends_on == ("task 19",)


def test_planner_reports_output_token_exhaustion_precisely():
    def exhausted(**kwargs):
        kwargs["stats"].stop_reason = "max_tokens"
        return "I need to continue reasoning about the plan"

    with pytest.raises(GoalPlanningError, match="exhausted its 32000-token output budget"):
        plan_tasks("large goal", "pytest -q", planner_runner=exhausted)


def test_planner_prompt_keeps_evidence_visible_when_manifest_has_a_large_repo_map():
    manifest = {
        "base_revision": "abc123",
        "revision": 2,
        "repo_files": [f"generated/file_{index}.ts" for index in range(50_000)],
        "shards": [{"path": f"generated/file_{index}.ts", "symbols": ["unused"]} for index in range(50_000)],
        "evidence": [{
            "id": "E1",
            "path": "node_tui/docs/requirements.md",
            "claim": "Queued messages must be submitted after the active run completes.",
            "symbol": "P0-01",
            "lines": [53, 112],
            "source_job": "requirement-1",
        }, *[{
            "id": f"E{index}",
            "path": f"node_tui/src/feature_{index}.ts",
            "claim": f"Independent deliverable {index} must be implemented.",
            "lines": [index, index],
            "source_job": "requirement-1",
        } for index in range(2, 61)]],
        "jobs": [{"id": "requirement-1", "role": "requirement", "status": "done"}],
    }

    prompt = build_plan_prompt(
        "implement node_tui/docs/requirements.md",
        "python -m pytest -q",
        TestCatalog(selectors=tuple(f"tests/test_{index}.py::test_case" for index in range(200))),
        manifest,
    )

    assert '"id": "E1"' in prompt
    assert "Queued messages must be submitted" in prompt
    assert '"id": "E60"' in prompt
    assert "node_tui/docs/requirements.md" in prompt
    assert "scope_candidates or evidence.path" in prompt
    assert "generated/file_49999.ts" not in prompt
    assert "There is no target Task count" in prompt
    assert len(prompt) < 100_000


def test_engine_rejects_illegal_transition():
    state = GoalState.new(target="t", verification="v", workspace="w")
    try:
        GoalEngine().transition(state, GoalPhase.ACT, "illegal")
        assert False
    except GoalTransitionError:
        pass


def test_store_rejects_prior_goal_schema(tmp_path):
    workspace = str(tmp_path / "ws")
    state = GoalState.new(target="t", verification="v", workspace=workspace)
    save_goal(state)
    assert load_goal(workspace).schema_version == GOAL_SCHEMA_VERSION
    path = goal_path(workspace)
    path.write_text('{"schema_version": 1, "feature_id": "feat_old"}', encoding="utf-8")
    try:
        load_goal(workspace)
        assert False
    except GoalStoreError as exc:
        assert exc.code == "unsupported_schema"


def test_store_migrates_v5_goal_with_no_execution_replan_checkpoint(tmp_path):
    workspace = str(tmp_path / "ws")
    state = GoalState.new(target="t", verification="v", workspace=workspace)
    payload = state.to_dict()
    payload["schema_version"] = 5
    payload.pop("execution_replan_checkpoint")
    path = goal_path(workspace)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_goal(workspace)

    assert loaded is not None
    assert loaded.schema_version == GOAL_SCHEMA_VERSION
    assert loaded.execution_replan_checkpoint == {}


def test_load_goal_reclassifies_legacy_repair_json_error(tmp_path):
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.status = "paused"
    state.phase = "paused"
    state.resume_phase = "repair_plan"
    state.stop_reason = "provider_unavailable"
    state.last_error = "repair planner returned no JSON"
    save_goal(state)

    loaded = load_goal(tmp_path)

    assert loaded is not None
    assert loaded.stop_reason == "repair_plan_format_error"


def test_load_goal_recovers_resolved_permission_boundary_to_verify(tmp_path):
    state = GoalState.new(target="recover", verification="pytest -q", workspace=str(tmp_path))
    state.status = GoalStatus.PAUSED.value
    state.phase = GoalPhase.PAUSED.value
    state.resume_phase = GoalPhase.ACT.value
    state.stop_reason = StopReason.permission_wait.value
    state.current_task_id = "task-current"
    state.permission_boundary_attempts = {"task-current": 1}
    state.last_error = "permission required"
    state.supervision = {
        "latest": {
            "trigger": "permission_boundary",
            "action": "continue",
            "unavailable": False,
            "stale": False,
        }
    }
    save_goal(state)

    loaded = load_goal(tmp_path)

    assert loaded is not None
    assert loaded.phase == GoalPhase.PAUSED.value
    assert loaded.resume_phase == GoalPhase.VERIFY.value
    assert loaded.stop_reason == StopReason.permission_wait.value
    assert loaded.last_error is None
    assert loaded.permission_boundary_attempts == {}


def test_load_goal_reclassifies_legacy_unavailable_supervisor_permission_wait(tmp_path):
    state = GoalState.new(target="recover", verification="pytest -q", workspace=str(tmp_path))
    state.status = GoalStatus.PAUSED.value
    state.phase = GoalPhase.PAUSED.value
    state.resume_phase = GoalPhase.ACT.value
    state.stop_reason = StopReason.permission_wait.value
    state.current_task_id = "task-current"
    state.permission_boundary_attempts = {"task-current": 1}
    state.supervision = {
        "latest": {
            "trigger": "permission_boundary",
            "action": "watch",
            "unavailable": True,
        }
    }
    save_goal(state)

    loaded = load_goal(tmp_path)

    assert loaded is not None
    assert loaded.stop_reason == StopReason.provider_unavailable.value
    assert loaded.permission_boundary_attempts == {}


def test_goal_lease_allows_only_one_live_runner_per_workspace(tmp_path):
    state = GoalState.new(target="t", verification="v", workspace=str(tmp_path))
    token = acquire_goal_lease(state)
    try:
        with pytest.raises(GoalLeaseError):
            acquire_goal_lease(state)
    finally:
        release_goal_lease(state, token)
    second_token = acquire_goal_lease(state)
    release_goal_lease(state, second_token)


def test_goal_agents_use_the_configured_plan_run_eval_profiles():
    """Goal planning, execution, and independent evaluation stay separated."""
    from harness.agents.registry import get_agent_profile, validate_agent_model

    expected = {
        "goal_intake": ("gpt-5.6-terra", "high"),
        "goal_discovery_requirement": ("mimo-v2.5", None),
        "goal_discovery_architecture": ("mimo-v2.5", None),
        "goal_discovery_implementation": ("mimo-v2.5", None),
        "goal_discovery_tests": ("mimo-v2.5", None),
        "goal_discovery_history": ("mimo-v2.5", None),
        "goal_planner": ("deepseek-v4-pro", "max"),
        "goal_repair_planner": ("gpt-5.6-terra", "high"),
        "goal_test_impact": ("mimo-v2.5", None),
        "goal_test_writer": ("gpt-5.6-terra", "high"),
        "goal_supervisor": ("gpt-5.6-sol", "high"),
        "goal_worker": ("gpt-5.6-terra", "high"),
        "evaluator": ("gpt-5.6-terra", "high"),
    }
    for agent_type, (model_id, reasoning_effort) in expected.items():
        profile = get_agent_profile(agent_type)
        assert profile is not None
        assert profile.model_id == model_id
        assert profile.reasoning_effort == reasoning_effort
        assert validate_agent_model(agent_type) is None
def test_windows_pid_probe_uses_native_fallback_when_kill_zero_is_invalid(monkeypatch):
    import harness.goal.store as store

    monkeypatch.setattr(store.os, "name", "nt")
    monkeypatch.setattr(store.os, "kill", lambda *_args: (_ for _ in ()).throw(SystemError("WinError 87")))
    monkeypatch.setattr(store, "_windows_pid_is_alive", lambda pid: pid == 123)

    assert store._pid_is_alive(123) is True
    assert store._pid_is_alive(124) is False
