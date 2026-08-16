"""Core tests for strict Goal task persistence and planning."""

from harness.goal.commands import parse_goal_command, parse_goal_subcommand
from harness.goal.engine import GoalEngine, GoalTransitionError
from harness.goal.models import GOAL_SCHEMA_VERSION, GoalPhase, GoalState
import pytest

from harness.goal.planner import GoalPlanningError, plan_tasks
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
        "goal_intake": ("deepseek-v4-pro", "max"),
        "goal_planner": ("deepseek-v4-pro", "max"),
        "goal_repair_planner": ("deepseek-v4-pro", "max"),
        "goal_test_impact": ("deepseek-v4-pro", "max"),
        "goal_test_writer": ("deepseek-v4-flash", "max"),
        "goal_worker": ("deepseek-v4-flash", "max"),
        "evaluator": ("mimo-v2.5-pro", None),
    }
    for agent_type, (model_id, reasoning_effort) in expected.items():
        profile = get_agent_profile(agent_type)
        assert profile is not None
        assert profile.model_id == model_id
        assert profile.reasoning_effort == reasoning_effort
        assert validate_agent_model(agent_type) is None
