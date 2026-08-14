"""Core tests for strict Goal task persistence and planning."""

from harness.goal.commands import parse_goal_command
from harness.goal.engine import GoalEngine, GoalTransitionError
from harness.goal.models import GOAL_SCHEMA_VERSION, GoalPhase, GoalState
from harness.goal.planner import plan_tasks
from harness.goal.store import GoalStoreError, goal_path, load_goal, save_goal


def test_parse_limit_flags_map_to_goalrequest_fields():
    result = parse_goal_command('/goal --verify "pytest -q" --max-rounds 5 --max-attempts 2 --max-failures 4 --timeout 600 -- fix bug')
    assert result["limits"]["max_rounds_per_attempt"] == 5
    assert result["limits"]["max_attempts"] == 2


def test_plan_tasks_fallback_requires_test_generation():
    plans = plan_tasks("fix everything", "pytest -q", planner_runner=lambda **_: "")
    assert len(plans) == 1
    assert plans[0].verification_spec.source == "needs_generation"


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
