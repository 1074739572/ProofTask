"""Contracts for the user-confirmed Goal intake flow."""

from __future__ import annotations

from harness.goal.commands import parse_goal_command
from harness.goal.draft import answer_draft, approve_draft, create_draft, load_draft


def _plan_json() -> str:
    return (
        '[{"name":"limit requests","behavior":"each user is limited",'
        '"acceptance_cases":[{"id":"AC1","given":"a user exceeds the limit",'
        '"when":"a request arrives","then":"the request is rejected"}],'
        '"test_selectors":[],"depends_on":[]}]'
    )


def test_bare_goal_creates_a_draft_instead_of_requiring_verify():
    parsed = parse_goal_command("/goal add per-user rate limits")

    assert parsed["action"] == "draft"
    assert parsed["verify"] is None
    assert parsed["target"] == "add per-user rate limits"
    assert parse_goal_command("/goal run")["action"] == "run"


def test_draft_waits_for_clarification_before_planning(tmp_path):
    draft = create_draft(
        "add rate limits",
        workspace=tmp_path,
        intake_runner=lambda **_: '{"questions":["Should limits be per user or per API key?"]}',
    )

    assert draft.status == "clarifying"
    assert draft.task_plan == []
    assert load_draft(tmp_path).unanswered_question

    ready = answer_draft("Per user.", workspace=tmp_path, planner_runner=lambda **_: _plan_json())

    assert ready.status == "ready"
    assert ready.task_plan[0]["verification_spec"]["source"] == "needs_generation"


def test_approval_requires_a_ready_plan_and_preserves_it(tmp_path):
    draft = create_draft(
        "add rate limits",
        workspace=tmp_path,
        verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":[]}',
        planner_runner=lambda **_: _plan_json(),
    )

    approved = approve_draft(workspace=tmp_path)

    assert draft.status == "ready"
    assert approved.status == "approved"
    assert approved.task_plan == draft.task_plan


def test_approved_draft_plan_seeds_the_runner(monkeypatch, tmp_path):
    import harness.goal.runner as runner_mod
    from harness.goal.runner import GoalRequest

    monkeypatch.setattr(runner_mod, "_runner", None)
    monkeypatch.setattr(runner_mod, "load_goal", lambda: None)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "archive_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(runner_mod, "workspace_generation", lambda: 0)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod.GoalRunner, "start", lambda self: None)

    plan = [{"name": "limit requests", "behavior": "limit each user", "acceptance_cases": [{"id": "AC1", "given": "x", "when": "y", "then": "z"}], "verification_spec": {"source": "needs_generation"}}]
    state = runner_mod.start_goal(GoalRequest(target="limit", verification="python -m pytest -q", task_plan=plan), history=[], context={}, binding=None)

    assert state.task_plan == plan
    assert state.execution_approved is True


def test_execution_approval_resumes_from_the_test_review_pause(monkeypatch, tmp_path):
    import harness.goal.runner as runner_mod
    from harness.goal.models import GoalPhase, GoalState, GoalStatus

    state = GoalState.new(target="limit", verification="python -m pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = GoalStatus.PAUSED.value
    state.stop_reason = "user_approval_required"
    state.execution_approved = False
    monkeypatch.setattr(runner_mod, "_runner", None)
    monkeypatch.setattr(runner_mod, "load_goal", lambda: state)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod.GoalRunner, "start", lambda self: None)

    resumed = runner_mod.resume_goal(history=[], context={}, binding=None)

    assert resumed.phase == GoalPhase.SELECT_TASK.value
    assert resumed.status == GoalStatus.RUNNING.value
    assert resumed.execution_approved is True
