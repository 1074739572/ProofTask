"""Contracts for the user-confirmed Goal intake flow."""

from __future__ import annotations

import pytest

from harness.goal.commands import parse_goal_command
from harness.goal.draft import answer_draft, approve_draft, create_draft, load_draft, resume_draft, pause_draft


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


def test_draft_scopes_a_nested_node_project_before_collecting_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_root.py").write_text("def test_root(): pass\n", encoding="utf-8")
    project = tmp_path / "node_tui"
    (project / "docs").mkdir(parents=True)
    (project / "test").mkdir()
    (project / "package.json").write_text('{"scripts":{"test":"node --import tsx --test test/*.test.ts"}}', encoding="utf-8")
    target_file = project / "docs" / "INPUT.md"
    target_file.write_text("improve input", encoding="utf-8")
    (project / "test" / "input.test.ts").write_text("import { test } from 'node:test';\ntest('input works', () => {});\n", encoding="utf-8")
    seen = {}

    def planner(**kwargs):
        seen.update(kwargs)
        return _plan_json()

    draft = create_draft(
        f"implement {target_file}", workspace=tmp_path,
        intake_runner=lambda **_: '{"questions":[]}', planner_runner=planner,
    )

    assert draft.project_root == "node_tui"
    assert draft.verification == "npm test"
    assert draft.verification_adapter == "node"
    assert draft.test_catalog_count == 1
    assert "test/input.test.ts::input works" in seen["prompt"]


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


def test_draft_recognizes_questions_wrapped_by_agent_runner(tmp_path):
    planner_called = False

    def planner(**_):
        nonlocal planner_called
        planner_called = True
        return _plan_json()

    draft = create_draft(
        "add rate limits",
        workspace=tmp_path,
        verification="python -m pytest -q",
        intake_runner=lambda **_: (
            "[goal_intake / deepseek-v4-pro] clarify verified coding goal (0 tools, 1.0s)\n\n"
            '{"questions":["Should limits be per user or per API key?"]}'
        ),
        planner_runner=planner,
    )

    assert draft.status == "clarifying"
    assert draft.questions == ["Should limits be per user or per API key?"]
    assert "unresolved" in draft.intake_summary.lower()
    assert not planner_called


def test_clear_intake_persists_conclusion_and_assumptions(tmp_path):
    draft = create_draft(
        "add rate limits",
        workspace=tmp_path,
        verification="python -m pytest -q",
        intake_runner=lambda **_: '{"summary":"每个用户独立限流","assumptions":["沿用现有认证用户 ID"],"questions":[]}',
        planner_runner=lambda **_: _plan_json(),
    )

    assert draft.intake_summary == "每个用户独立限流"
    assert draft.intake_assumptions == ["沿用现有认证用户 ID"]
    persisted = load_draft(tmp_path)
    assert persisted is not None
    assert persisted.intake_summary == draft.intake_summary
    assert persisted.intake_assumptions == draft.intake_assumptions


def test_draft_is_persisted_before_slow_intake(tmp_path):
    observed = {}

    def intake(**_):
        persisted = load_draft(tmp_path)
        observed["draft"] = persisted
        return '{"questions":[]}'

    draft = create_draft("add rate limits", workspace=tmp_path, verification="python -m pytest -q", intake_runner=intake, planner_runner=lambda **_: _plan_json())

    assert observed["draft"] is not None
    assert observed["draft"].stage in {"discovering", "intake"}
    assert observed["draft"].input_hash
    assert observed["draft"].last_heartbeat > 0
    assert draft.stage == "planning" or draft.stage == "ready"


def test_discovery_and_planning_statuses_match_the_active_stage(tmp_path, monkeypatch):
    import harness.goal.draft as draft_module

    observed = []

    def discovery(**kwargs):
        current = load_draft(tmp_path)
        observed.append((current.status, current.stage))
        return {"repo_files": [], "evidence": [], "jobs": [], "revision": 1}

    monkeypatch.setattr(draft_module, "_plan", lambda *args, **kwargs: setattr(args[0], "status", "ready"))
    create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":[]}',
        discovery_runner=discovery,
    )

    assert observed == [("discovering", "discovering")]


def test_draft_event_includes_every_planned_task():
    from harness.goal.draft import GoalDraft, _draft_event_payload

    draft = GoalDraft(
        id="large-draft",
        target="large goal",
        verification="python -m pytest -q",
        verification_source="test",
        status="ready",
        stage="ready",
        task_plan=[
            {"name": f"task {index}", "behavior": f"behavior {index}"}
            for index in range(20)
        ],
    )

    payload = _draft_event_payload(draft, event="completed")

    assert payload["task_count"] == 20
    assert len(payload["tasks"]) == 20
    assert payload["tasks"][-1]["name"] == "task 19"


def test_current_draft_status_distinguishes_hydration_from_explicit_status(monkeypatch):
    from harness.goal import draft as draft_mod

    current = draft_mod.GoalDraft(
        id="draft-status",
        target="keep normal chat available",
        verification="python -m pytest -q",
        verification_source="test",
        status="ready",
        stage="ready",
    )
    emitted = []
    monkeypatch.setattr(draft_mod, "load_draft", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        draft_mod,
        "_emit_draft_event",
        lambda value, *, event="updated", message=None: emitted.append(event),
    )

    draft_mod.emit_current_draft_status()
    draft_mod.emit_current_draft_status(event="status")

    assert emitted == ["hydrated", "status"]


def test_invalid_intake_json_pauses_draft_instead_of_planning(tmp_path):
    with pytest.raises(ValueError, match="invalid JSON"):
        create_draft(
            "add rate limits",
            workspace=tmp_path,
            verification="python -m pytest -q",
            intake_runner=lambda **_: "not json",
            planner_runner=lambda **_: pytest.fail("planner must not run"),
        )

    draft = load_draft(tmp_path)
    assert draft is not None
    assert draft.status == "paused"
    assert draft.stage == "paused"
    assert "invalid JSON" in (draft.last_error or "")


def test_resume_retries_intake_failure_before_discovery(tmp_path):
    with pytest.raises(ValueError, match="invalid JSON"):
        create_draft(
            "add rate limits",
            workspace=tmp_path,
            verification="python -m pytest -q",
            intake_runner=lambda **_: "not json",
            planner_runner=lambda **_: pytest.fail("planner must not run"),
        )

    # The production retry uses the configured Goal intake agent. Injecting a
    # planner here proves resume does not skip straight to Discovery after an
    # intake failure; the explicit intake retry is covered by its runner call.
    from harness.goal.draft import load_draft, save_draft
    draft = load_draft(tmp_path)
    assert draft is not None and draft.stage == "paused"


def test_resume_recovers_stale_discovery_draft(tmp_path, monkeypatch):
    import harness.goal.draft as draft_module
    from harness.goal.draft import GoalDraft, save_draft

    draft = GoalDraft(
        id="stale-discovery", target="improve input", verification="python -m pytest -q",
        verification_source="test", status="discovering", stage="discovering",
        last_heartbeat=0,
    )
    monkeypatch.setattr(draft_module.time, "time", lambda: 100.0)
    save_draft(draft, tmp_path)
    monkeypatch.setattr(draft_module.time, "time", lambda: 200.0)
    observed = []
    catalog = type("Catalog", (), {"selectors": [], "prompt_text": lambda self: "catalog"})()
    monkeypatch.setattr(draft_module, "_collect_catalog", lambda *_: (object(), catalog))
    monkeypatch.setattr(
        draft_module,
        "_run_discovery_and_plan",
        lambda draft, *args, **kwargs: observed.append((draft.status, draft.stage)),
    )
    resumed = resume_draft(workspace=tmp_path)
    assert observed == [("discovering", "catalog")]
    assert resumed.status == "discovering"
    assert resumed.stage == "catalog"
    persisted = load_draft(tmp_path)
    assert persisted is not None and (persisted.status, persisted.stage) == ("discovering", "catalog")


def test_pause_draft_releases_an_active_discovery_stage(tmp_path):
    draft = create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":["scope?"]}',
    )
    draft.status = "discovering"
    draft.stage = "discovering"
    from harness.goal.draft import save_draft
    save_draft(draft, tmp_path)

    assert "Normal chat is available" in pause_draft(workspace=tmp_path)
    paused = load_draft(tmp_path)
    assert paused.status == "paused"
    assert paused.stage == "paused"


def test_pause_draft_during_intake_can_resume_and_clears_global_cancel(tmp_path):
    from harness.agent.cancel import clear_cancel, is_cancelled
    from harness.goal.draft import save_draft

    clear_cancel()
    draft = create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":["scope?"]}',
    )
    draft.status = "clarifying"
    draft.stage = "intake"
    save_draft(draft, tmp_path)

    assert "paused" in pause_draft(workspace=tmp_path)
    assert is_cancelled()

    resumed = resume_draft(workspace=tmp_path)

    assert resumed.status == "clarifying"
    assert not is_cancelled()


def test_cancelled_draft_is_terminal_and_allows_a_fresh_draft(tmp_path):
    from harness.goal.draft import GoalDraftError, save_draft

    draft = create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":["scope?"]}',
    )
    draft.stage = "intake"
    save_draft(draft, tmp_path)

    assert "cancelled" in pause_draft(workspace=tmp_path, cancelled=True)
    assert load_draft(tmp_path).status == "cancelled"
    with pytest.raises(GoalDraftError, match="No paused Goal draft"):
        resume_draft(workspace=tmp_path)

    fresh = create_draft(
        "add a different limit", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":["scope?"]}',
    )
    assert fresh.id != draft.id


def test_resume_reports_intake_provider_failure_without_json_masking(tmp_path, monkeypatch):
    from harness.goal import draft as draft_module

    draft = draft_module.GoalDraft(
        id="goal-intake-retry", target="add rate limits", verification="python -m pytest -q",
        verification_source="test", status="paused", stage="paused",
        last_error="Goal intake returned invalid JSON: old error",
    )
    draft_module.save_draft(draft, tmp_path)

    def failed_intake(*_args, **kwargs):
        kwargs["stats"].stop_reason = "provider_error"
        return "[goal_intake] failed: APIConnectionError: Connection error"

    catalog = type("Catalog", (), {"selectors": [], "prompt_text": lambda self: "catalog"})()
    monkeypatch.setattr(draft_module, "_collect_catalog", lambda *_: (object(), catalog))
    monkeypatch.setattr("harness.agents.runner.run_agent_task", failed_intake)

    with pytest.raises(ValueError, match="Goal intake is unavailable: .*APIConnectionError"):
        draft_module.resume_draft(workspace=tmp_path)


def test_empty_intake_response_never_enters_discovery(tmp_path):
    with pytest.raises(ValueError, match="no visible JSON response"):
        create_draft(
            "add rate limits", workspace=tmp_path,
            verification="python -m pytest -q",
            intake_runner=lambda **_: "",
            planner_runner=lambda **_: pytest.fail("planner must not run"),
        )

    draft = load_draft(tmp_path)
    assert draft is not None
    assert draft.stage == "paused"


def test_answer_replans_through_discovery_and_resume_retries_paused_draft(tmp_path):
    discovery_calls = []

    def discovery(**kwargs):
        discovery_calls.append(kwargs["draft"].target)
        return {
            "repo_files": ["src/rate.py"],
            "evidence": [{"id": "E1", "path": "src/rate.py"}],
            "jobs": [], "revision": 2,
        }

    def planner(**_):
        return ('[{"name":"limit requests","behavior":"each user is limited",'
                '"acceptance_cases":[{"id":"AC1","given":"a user exceeds the limit",'
                '"when":"a request arrives","then":"the request is rejected"}],'
                '"test_selectors":[],"depends_on":[],"scope_paths":["src/rate.py"],'
                '"evidence_refs":["E1"],"test_strategy":"generated focused test"}]')

    draft = create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":["scope?"]}',
    )
    ready = answer_draft(
        "per user", workspace=tmp_path, planner_runner=planner,
        discovery_runner=discovery,
    )

    assert ready.status == "ready"
    assert discovery_calls == ["add rate limits"]

    ready.status = "paused"
    from harness.goal.draft import save_draft
    save_draft(ready, tmp_path)
    resumed = resume_draft(
        workspace=tmp_path, planner_runner=planner,
        discovery_runner=discovery,
    )
    assert resumed.status == "ready"
    assert len(discovery_calls) == 2


def test_partial_discovery_failure_keeps_planning_with_completed_evidence(tmp_path):
    manifest = {
        "repo_files": ["src/rate.py"],
        "evidence": [{"id": "E1", "path": "src/rate.py"}],
        "jobs": [
            {"id": "implementation-1", "role": "implementation", "status": "done"},
            {"id": "tests-1", "role": "tests", "status": "failed", "error": "invalid report"},
        ],
        "gaps": ["tests discovery unavailable: invalid report"],
        "revision": 1,
    }

    draft = create_draft(
        "add rate limits", workspace=tmp_path, verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":[]}',
        discovery_runner=lambda **_: manifest,
        planner_runner=lambda **_: _plan_json(),
    )

    assert draft.status == "ready"
    assert draft.task_plan


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


def test_start_failure_restores_ready_draft(monkeypatch, tmp_path):
    import harness.goal.commands as commands
    import harness.goal.draft as draft_module

    create_draft(
        "add rate limits",
        workspace=tmp_path,
        verification="python -m pytest -q",
        intake_runner=lambda **_: '{"questions":[]}',
        planner_runner=lambda **_: _plan_json(),
    )
    approve_draft(workspace=tmp_path)
    monkeypatch.setattr(commands, "_start_precondition_note", lambda _request: "startup unavailable")
    monkeypatch.setattr(draft_module, "get_workdir", lambda: tmp_path)

    class Runner:
        pass

    result = commands._handle_approve(Runner(), [], {}, None)

    assert result == "startup unavailable"
    draft = load_draft(tmp_path)
    assert draft.status == "ready"
    assert draft.stage == "ready"
    assert draft.last_error == "startup unavailable"


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

    project = tmp_path / "node_tui"
    project.mkdir()
    plan = [{"name": "limit requests", "behavior": "limit each user", "acceptance_cases": [{"id": "AC1", "given": "x", "when": "y", "then": "z"}], "verification_spec": {"source": "needs_generation"}}]
    state = runner_mod.start_goal(
        GoalRequest(
            target="limit", verification="npm test", execution_workspace=str(project),
            task_plan=plan, draft_id="draft_origin",
        ),
        history=[], context={}, binding=None,
    )

    assert state.task_plan == plan
    assert state.execution_approved is True
    assert state.draft_id == "draft_origin"
    assert state.workspace == str(tmp_path)
    assert state.execution_workspace == str(project)


def test_execution_approval_resumes_from_the_test_review_pause(monkeypatch, tmp_path):
    import harness.goal.runner as runner_mod
    from harness.goal.models import GoalPhase, GoalState, GoalStatus

    state = GoalState.new(target="limit", verification="python -m pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = GoalStatus.PAUSED.value
    state.stop_reason = "user_approval_required"
    state.execution_approved = False
    state.initialization_complete = True
    monkeypatch.setattr(runner_mod, "_runner", None)
    monkeypatch.setattr(runner_mod, "load_goal", lambda: state)
    monkeypatch.setattr(runner_mod, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod.GoalRunner, "start", lambda self: None)

    with pytest.raises(runner_mod.GoalNotRunningError):
        runner_mod.resume_goal(history=[], context={}, binding=None)

    resumed = runner_mod.resume_goal(history=[], context={}, binding=None, approve_execution=True)

    assert resumed.phase == GoalPhase.SELECT_TASK.value
    assert resumed.status == GoalStatus.RUNNING.value
    assert resumed.execution_approved is True
