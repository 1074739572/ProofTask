from types import SimpleNamespace

from harness.agents.runner import AgentTaskStats
from harness.goal.impact import ImpactDecision, review_test_impact
from harness.goal.models import GoalPhase, GoalState, GoalStatus, StopReason
from harness.goal.store import load_goal, save_goal


def _task(task_id: str, subject: str = "task"):
    return SimpleNamespace(
        id=task_id,
        subject=subject,
        description="behavior",
        blockedBy=[],
        acceptance_cases=[],
    )


def test_impact_review_retries_non_json_output_once(tmp_path, monkeypatch):
    import harness.goal.impact as impact

    state = SimpleNamespace(goal_contract={}, id="goal-test")
    completed = _task("done")
    pending = [_task("next")]
    calls = []
    monkeypatch.setattr(impact, "load_test_map", lambda state: [])

    def runner(**kwargs):
        calls.append(kwargs)
        return "plain prose" if len(calls) == 1 else '{"action":"add_tests","task_id":"next","reason":"shared interface"}'

    decision = review_test_impact(
        state,
        completed,
        pending,
        cwd=str(tmp_path),
        stats=AgentTaskStats(),
        runner=runner,
    )

    assert len(calls) == 2
    assert "JSON correction" in calls[1]["description"]
    assert decision.action == "add_tests"
    assert decision.task_id == "next"
    assert decision.parse_attempts == 2
    assert not decision.unavailable
    assert not decision.format_error


def test_impact_review_reports_format_error_after_correction(tmp_path, monkeypatch):
    import harness.goal.impact as impact

    monkeypatch.setattr(impact, "load_test_map", lambda state: [])
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return "still prose"

    decision = review_test_impact(
        SimpleNamespace(goal_contract={}, id="goal-test"),
        _task("done"),
        [_task("next")],
        cwd=str(tmp_path),
        stats=AgentTaskStats(),
        runner=runner,
    )

    assert len(calls) == 2
    assert decision.format_error
    assert not decision.unavailable
    assert decision.parse_attempts == 2
    assert "after JSON correction" in decision.reason


def test_impact_review_keeps_provider_failure_distinct(tmp_path, monkeypatch):
    import harness.goal.impact as impact

    monkeypatch.setattr(impact, "load_test_map", lambda state: [])
    stats = AgentTaskStats(stop_reason="provider_error")
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return "[goal_test_impact] failed: APIConnectionError"

    decision = review_test_impact(
        SimpleNamespace(goal_contract={}, id="goal-test"),
        _task("done"),
        [_task("next")],
        cwd=str(tmp_path),
        stats=stats,
        runner=runner,
    )

    assert len(calls) == 1
    assert decision.unavailable
    assert not decision.format_error
    assert decision.reason == "impact reviewer provider unavailable"


def test_load_goal_reclassifies_legacy_impact_json_error(tmp_path):
    state = GoalState.new(target="impact", verification="pytest -q", workspace=str(tmp_path))
    state.status = GoalStatus.PAUSED.value
    state.phase = GoalPhase.PAUSED.value
    state.resume_phase = GoalPhase.IMPACT_REVIEW.value
    state.stop_reason = StopReason.provider_unavailable.value
    state.last_error = "impact reviewer returned no JSON"
    save_goal(state)

    loaded = load_goal(tmp_path)

    assert loaded is not None
    assert loaded.stop_reason == StopReason.impact_review_format_error.value


def test_impact_review_persists_context_for_the_affected_task(tmp_path, monkeypatch):
    import harness.goal.impact as impact
    import harness.goal.memory as memory
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    completed = tasks.create_task("permission engine", "change shared permission path")
    target = tasks.create_task("approval flow", "preserve remembered approval")
    state = GoalState.new(target="permission behavior", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [completed.id, target.id]
    state.current_task_id = completed.id
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(memory, "append_decisions", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        impact,
        "review_test_impact",
        lambda *args, **kwargs: ImpactDecision(
            action="add_tests",
            task_id=target.id,
            reason="the shared permission path now gates approval persistence",
        ),
    )

    runner_mod.GoalRunner(state=state, history=[], context={}, binding=None)._impact_review(state)

    spec = tasks.load_task(target.id).verification_spec
    assert spec["owners"] == [completed.id, target.id]
    assert spec["impact_context"] == [
        {
            "source_task_id": completed.id,
            "source_task_subject": "permission engine",
            "reason": "the shared permission path now gates approval persistence",
            "required_coverage": (
                "Add focused interaction coverage for this upstream Task and the target Task "
                "before implementation; do not only repeat task-local tests."
            ),
        }
    ]
