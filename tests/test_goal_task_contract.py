"""Contracts for the latest Goal -> Task -> evidence model."""

from __future__ import annotations

from harness.goal.planner import TaskPlan, parse_plan
from harness.goal.models import GoalPhase, GoalState
from harness.goal.runner import GoalRunner
from harness.verification.catalog import TestCatalog


def _catalog() -> TestCatalog:
    return TestCatalog(
        selectors=("tests/test_api.py::test_lists_all_pages",),
        test_files=("tests/test_api.py",),
    )


def test_plan_binds_only_catalog_selectors():
    plans = parse_plan(
        '[{"name":"pages","behavior":"all pages return",'
        '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
        '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[]}]',
        test_catalog=_catalog(),
    )
    assert plans is not None
    assert isinstance(plans[0], TaskPlan)
    assert plans[0].verification_spec.source == "discovered"
    assert plans[0].verification_spec.selectors == ("tests/test_api.py::test_lists_all_pages",)


def test_unknown_selector_requires_test_generation():
    plans = parse_plan(
        '[{"name":"new","behavior":"new behavior",'
        '"acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"new result"}],'
        '"test_selectors":["tests/invented.py::test_new"],"depends_on":[]}]',
        test_catalog=_catalog(),
    )
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"
    assert not plans[0].verification_spec.command


def test_legacy_verification_command_is_not_a_task_binding():
    plans = parse_plan('[{"name":"x","behavior":"b","acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"result"}],"verification":"pytest -q","depends_on":[]}]')
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"


def _needs_generation_task(tmp_path, monkeypatch):
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task(
        "new behavior",
        "adds missing behavior",
        acceptance_cases=[{"id": "AC1", "given": "input", "when": "called", "then": "new result"}],
        verification_spec={"source": "needs_generation"},
    )
    state = GoalState.new(target="new behavior", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.phase = GoalPhase.PREPARE_TESTS.value
    return task, state


def test_test_generation_uses_writer_and_requires_failing_baseline(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: calls.append(kwargs["agent_type"]) or '{"test_selectors":["tests/test_new.py::test_new"]}',
    )
    monkeypatch.setattr(
        runner_mod,
        "collect_pytest_catalog",
        lambda workspace: TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    )
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    bound = load_task(task.id)
    assert calls == ["goal_test_writer"]
    assert state.phase == GoalPhase.SELECT_TASK.value
    assert bound.verification_spec["source"] == "generated"
    assert bound.verification_spec["baseline_result"] == "failing"


def test_test_generation_rejects_a_passing_baseline(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", lambda **kwargs: '{"test_selectors":["tests/test_new.py::test_new"]}')
    monkeypatch.setattr(
        runner_mod,
        "collect_pytest_catalog",
        lambda workspace: TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    )
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": True, "error": None, "timed_out": False})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert load_task(task.id).verification_state == "needs_generation"
    assert "passed before implementation" in state.last_error


def test_goal_forces_clean_enforcement_and_reverifies_all_tasks(monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.task_ids = ["task_a", "task_b"]
    state.current_task_id = "task_a"
    state.phase = GoalPhase.CLEAN_CHECK.value
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    modes = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr("harness.tasks.complete_task", lambda task_id, **kwargs: modes.append(kwargs["clean_check_mode"]) or "Completed " + task_id)
    runner._clean_check(state)
    assert modes == ["enforce"]

    state.phase = GoalPhase.FULL_VERIFY.value
    reverified = []
    monkeypatch.setattr(runner_mod, "reverify_task_command", lambda task_id, **kwargs: reverified.append(task_id) or type("Task", (), {"verification_state": "passing", "last_error": None})())
    monkeypatch.setattr(runner_mod, "run_verification", lambda *args, **kwargs: type("Result", (), {"passed": True, "error": None, "exit_code": 0})())
    monkeypatch.setattr(
        runner_mod,
        "evidence_from_result",
        lambda *args, **kwargs: type("Evidence", (), {"to_dict": lambda self: {"exit_code": 0, "stdout_tail": "3 passed", "duration_ms": 12.5, "code_snapshot": "abc"}})(),
    )
    runner._full_verify(state)
    assert reverified == ["task_a", "task_b"]
    assert state.phase == GoalPhase.DONE.value
    assert state.final_verification["status"] == "passed"
    assert state.final_verification["exit_code"] == 0


def test_goal_persists_global_regression_failure_evidence(monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.task_ids = ["task_a"]
    state.phase = GoalPhase.FULL_VERIFY.value
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda event_type, state: None)
    monkeypatch.setattr(runner_mod, "reverify_task_command", lambda *args, **kwargs: type("Task", (), {"verification_state": "passing", "last_error": None})())
    monkeypatch.setattr(runner_mod, "run_verification", lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "exit_code": 1})())
    monkeypatch.setattr(
        runner_mod,
        "evidence_from_result",
        lambda *args, **kwargs: type("Evidence", (), {"to_dict": lambda self: {"exit_code": 1, "stdout_tail": "1 failed", "duration_ms": 21.0, "code_snapshot": "def"}})(),
    )

    runner._full_verify(state)

    assert state.phase == GoalPhase.FAILED.value
    assert state.final_verification["status"] == "failed"
    assert state.final_verification["exit_code"] == 1
    assert state.final_verification["stdout_tail"] == "1 failed"


def test_goal_event_snapshot_exposes_task_contract_for_terminal_ui(tmp_path, monkeypatch):
    from harness.goal.runner import goal_event_payload

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    state.phase = GoalPhase.VERIFY.value
    payload = goal_event_payload(state)

    assert payload["current_task_id"] == task.id
    assert payload["verification"] == "pytest -q"
    assert payload["stop_reason"] is None
    assert payload["final_verification"] is None
    assert payload["tasks"] == [
        {
            "id": task.id,
            "subject": "new behavior",
            "status": "pending",
            "verification_state": "needs_generation",
            "blocked_by": [],
            "acceptance_cases": task.acceptance_cases,
            "verification_spec": task.verification_spec,
            "evidence_count": 0,
            "latest_evidence": None,
            "last_error": None,
        }
    ]


def test_goal_event_snapshot_sends_only_bounded_latest_evidence(tmp_path, monkeypatch):
    import harness.tasks as tasks
    from harness.goal.runner import goal_event_payload

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    task.evidence = [
        {"command": "pytest -q tests/test_api.py", "exit_code": 1, "stdout_tail": "old"},
        {
            "command": "pytest -q tests/test_api.py",
            "exit_code": 0,
            "stdout_tail": "x" * 2000,
            "duration_ms": 125.5,
            "verified_by": "runner",
            "code_snapshot": "abc123:clean",
            "selectors": ["tests/test_api.py::test_ok"],
            "collected_count": 1,
        },
    ]
    tasks.save_task(task)

    snapshot = goal_event_payload(state)["tasks"][0]

    assert snapshot["evidence_count"] == 2
    assert snapshot["latest_evidence"]["exit_code"] == 0
    assert snapshot["latest_evidence"]["selectors"] == ["tests/test_api.py::test_ok"]
    assert len(snapshot["latest_evidence"]["stdout_tail"]) == 1600


def test_goal_started_snapshot_is_emitted_before_worker_thread_runs(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    order = []
    monkeypatch.setattr(runner_mod, "_runner", None)
    monkeypatch.setattr(runner_mod, "load_goal", lambda: None)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(runner_mod, "workspace_generation", lambda: 0)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda event_type, state: order.append(event_type))
    monkeypatch.setattr(runner_mod.GoalRunner, "start", lambda self: order.append("worker_started"))

    runner_mod.start_goal(
        runner_mod.GoalRequest(target="ship behavior", verification="pytest -q"),
        history=[],
        context={},
        binding=None,
    )

    assert order == ["goal_started", "worker_started"]


def test_goal_status_hydration_skips_old_terminal_goal(monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="ship behavior", verification="pytest -q", workspace=".")
    emitted = []
    monkeypatch.setattr(runner_mod, "load_goal", lambda: state)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda event_type, current: emitted.append((event_type, current.status)))

    runner_mod.emit_current_goal_status(include_terminal=False)
    assert emitted == [("goal_status", "running")]

    state.status = "done"
    runner_mod.emit_current_goal_status(include_terminal=False)
    assert emitted == [("goal_status", "running")]

    runner_mod.emit_current_goal_status(include_terminal=True)
    assert emitted[-1] == ("goal_status", "done")


def test_goal_snapshot_degrades_missing_task_state_without_breaking_runner(monkeypatch):
    from harness.goal.runner import goal_event_payload

    state = GoalState.new(target="ship behavior", verification="pytest -q", workspace=".")
    state.task_ids = ["task_missing"]

    def unreadable_task(_task_id):
        raise OSError("unreadable")

    monkeypatch.setattr("harness.tasks.load_task", unreadable_task)

    task = goal_event_payload(state)["tasks"][0]

    assert task["status"] == "missing"
    assert task["latest_evidence"] is None
    assert "could not be loaded" in task["last_error"]
