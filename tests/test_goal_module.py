"""Tests for the /goal module (L6): parsing, state model, planner, engine, store."""

import time

from harness.goal.commands import parse_goal_command
from harness.goal.engine import GoalEngine, GoalTransitionError
from harness.goal.models import GoalPhase, GoalStatus, GoalState
from harness.goal.planner import parse_plan, plan_features
from harness.goal.store import GoalStoreError, load_goal, save_goal


# --- commands ---------------------------------------------------------------

def test_parse_limit_flags_map_to_goalrequest_fields():
    """Every documented limit flag must map to a GoalRequest field name
    (regression: the old mapping raised TypeError in _handle_start)."""
    result = parse_goal_command(
        '/goal --verify "pytest -q" --max-rounds 5 --max-attempts 2 '
        "--max-failures 4 --timeout 600 -- fix bug"
    )
    assert result["action"] == "start"
    assert result["limits"] == {
        "max_rounds_per_attempt": 5,
        "max_attempts": 2,
        "max_consecutive_failures": 4,
        "max_duration_seconds": 600,
    }


def test_parse_single_limit_flags():
    assert parse_goal_command('/goal --verify "x" --max-rounds 40 -- t')["limits"] == {
        "max_rounds_per_attempt": 40
    }
    assert parse_goal_command('/goal --verify "x" --timeout 123 -- t')["limits"] == {
        "max_duration_seconds": 123
    }
    assert parse_goal_command('/goal --verify "x" --max-failures 1 -- t')["limits"] == {
        "max_consecutive_failures": 1
    }


def test_parse_rejects_bad_limits():
    r = parse_goal_command('/goal --verify "x" --max-rounds abc -- t')
    assert r["action"] == "usage" and "integer" in r["error"]
    r = parse_goal_command('/goal --verify "x" --max-rounds 0 -- t')
    assert r["action"] == "usage" and "positive" in r["error"]


def test_parse_subcommands():
    for sub in ("status", "pause", "resume", "cancel"):
        assert parse_goal_command(f"/goal {sub}")["action"] == sub
    assert parse_goal_command("/goal pause extra")["action"] == "usage"


# --- models -----------------------------------------------------------------

def test_total_rounds_scales_with_user_limits():
    default = GoalState.new(target="t", verification="v", workspace="w")
    assert default.max_total_rounds == 60  # 20 * 3 default
    scaled = GoalState.new(
        target="t", verification="v", workspace="w",
        max_rounds_per_attempt=40, max_attempts=3,
    )
    assert scaled.max_total_rounds == 120
    explicit = GoalState.new(
        target="t", verification="v", workspace="w", max_total_rounds=999
    )
    assert explicit.max_total_rounds == 999


def test_goal_ids_are_unique_within_a_second():
    ids = {GoalState.new(target="t", verification="v", workspace="w").id for _ in range(5)}
    assert len(ids) == 5


# --- planner ----------------------------------------------------------------

def test_parse_plan_rejects_self_and_forward_deps():
    ok = parse_plan(
        '[{"name": "a", "behavior": "do a", "depends_on": []},'
        ' {"name": "b", "behavior": "do b", "depends_on": ["a"]}]'
    )
    assert [p.name for p in ok] == ["a", "b"]
    assert parse_plan('[{"name": "a", "behavior": "do a", "depends_on": ["a"]}]') is None
    assert parse_plan(
        '[{"name": "a", "behavior": "do a", "depends_on": ["b"]},'
        ' {"name": "b", "behavior": "do b"}]'
    ) is None


def test_plan_features_falls_back_when_planner_crashes():
    def boom(*args, **kwargs):
        raise RuntimeError("planner crashed")

    plans = plan_features("fix everything", "pytest -q", planner_runner=boom)
    assert len(plans) == 1
    assert plans[0].name == "fix everything"


# --- engine -----------------------------------------------------------------

def test_engine_rejects_illegal_transition():
    state = GoalState.new(target="t", verification="v", workspace="w")
    engine = GoalEngine()
    # INITIALIZE only allows SELECT_FEATURE + terminal escapes.
    try:
        engine.transition(state, GoalPhase.ACT, "illegal")
        assert False, "expected GoalTransitionError"
    except GoalTransitionError:
        pass
    # DONE is terminal: no further transitions.
    engine.transition(state, GoalPhase.SELECT_FEATURE, "ok")
    engine.transition(state, GoalPhase.CLEAN_CHECK, "ok")
    engine.transition(state, GoalPhase.DONE, "ok")
    try:
        engine.transition(state, GoalPhase.ACT, "illegal")
        assert False, "expected GoalTransitionError"
    except GoalTransitionError:
        pass


def test_engine_allows_terminal_escapes_from_any_nonterminal_phase():
    state = GoalState.new(target="t", verification="v", workspace="w")
    GoalEngine().transition(state, GoalPhase.PAUSED, "user_pause")
    assert state.status == GoalStatus.PAUSED.value
    state2 = GoalState.new(target="t", verification="v", workspace="w")
    GoalEngine().transition(state2, GoalPhase.FAILED, "boom")
    assert state2.status == GoalStatus.FAILED.value


# --- store ------------------------------------------------------------------

def test_store_roundtrip_and_restart_normalization(tmp_path):
    workspace = str(tmp_path / "ws")
    state = GoalState.new(target="t", verification="v", workspace=workspace)
    save_goal(state)
    loaded = load_goal(workspace=workspace)
    assert loaded is not None and loaded.id == state.id
    # A running goal on disk must be normalized to paused (restart recovery).
    loaded.status = GoalStatus.RUNNING.value
    loaded.phase = GoalPhase.ACT.value
    save_goal(loaded)
    normalized = load_goal(workspace=workspace)
    assert normalized.status == GoalStatus.PAUSED.value
    assert normalized.phase == GoalPhase.PAUSED.value
    assert normalized.stop_reason == "process_restarted"


def test_store_raises_on_corrupt_file(tmp_path):
    workspace = str(tmp_path / "ws")
    import json
    from pathlib import Path

    from harness.goal.store import goal_path

    goal_path(workspace).parent.mkdir(parents=True)
    goal_path(workspace).write_text("{not json", encoding="utf-8")
    try:
        load_goal(workspace=workspace)
        assert False, "expected GoalStoreError"
    except GoalStoreError as exc:
        assert exc.code == "goal_state_corrupt"
