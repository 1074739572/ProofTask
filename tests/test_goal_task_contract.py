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
            '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[],'
            '"case_selectors":{"AC1":["tests/test_api.py::test_lists_all_pages"]}}]',
        test_catalog=_catalog(),
    )
    assert plans is not None
    assert isinstance(plans[0], TaskPlan)
    assert plans[0].verification_spec.source == "discovered"
    assert plans[0].verification_spec.selectors == ("tests/test_api.py::test_lists_all_pages",)


def test_plan_requires_explicit_case_mapping_when_mapping_is_present():
    raw = ('[{"name":"pages","behavior":"all pages return",'
           '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
           '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[],'
           '"case_selectors":{"AC2":["tests/test_api.py::test_lists_all_pages"]}}]')
    assert parse_plan(raw, test_catalog=_catalog()) is None


def test_worker_scope_gate_rejects_changed_dirty_files_and_new_outside_scope(monkeypatch, tmp_path):
    from harness.goal import runner as runner_mod
    from harness.tasks import Task

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    task = Task(
        id="task_scope", subject="x", description="x", status="in_progress", owner="goal:x",
        blockedBy=[], scope_paths=["src"], start_dirty_hashes={"README.md": "old"},
    )
    monkeypatch.setattr("harness.verification.snapshot.capture_dirty_file_hashes", lambda _workspace: {
        "README.md": "changed", "src/app.py": "new", "docs/extra.md": "new",
    })

    error = runner_mod.GoalRunner._validate_task_scope(state, task)

    assert error is not None
    assert "docs/extra.md" in error
    assert "README.md" in error


def test_plan_allows_a_discovered_directory_scope_for_new_files():
    manifest = {
        "repo_files": ["src/existing.py"],
        "evidence": [{"id": "E1", "path": "src/existing.py"}],
    }
    plans = parse_plan(
        '[{"name":"new module","behavior":"adds a module",'
        '"acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"result"}],'
        '"depends_on":[],"scope_paths":["src"],"evidence_refs":["E1"],'
        '"test_strategy":"new focused test"}]',
        test_catalog=_catalog(), discovery_manifest=manifest,
    )

    assert plans is not None
    assert plans[0].scope_paths == ("src",)


def test_plan_rejects_empty_acceptance_case_selector_mapping():
    raw = ('[{"name":"pages","behavior":"all pages return",'
           '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
           '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[],'
           '"case_selectors":{"AC1":[]}}]')

    assert parse_plan(raw, test_catalog=_catalog()) is None


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


def test_plan_keeps_only_installed_skill_names(monkeypatch):
    monkeypatch.setattr("harness.skills_loader.skill_names", lambda: ["systematic-debugging"])
    plans = parse_plan(
        '[{"name":"repair","behavior":"fix a regression",'
        '"acceptance_cases":[{"id":"AC1","given":"bad input","when":"called","then":"works"}],'
        '"skills":["systematic-debugging","unknown-skill"],"depends_on":[]}]',
        test_catalog=_catalog(),
    )

    assert plans is not None
    assert plans[0].skill_names == ("systematic-debugging",)


def test_plan_assigns_frontend_skills_when_planner_omits_them(monkeypatch):
    monkeypatch.setattr(
        "harness.skills_loader.skill_names",
        lambda: ["frontend-design", "webapp-testing", "test-driven-development"],
    )
    plans = parse_plan(
        '[{"name":"settings page","behavior":"add a frontend UI panel",'
        '"acceptance_cases":[{"id":"AC1","given":"a user","when":"opening settings","then":"panel renders"}],'
        '"depends_on":[]}]',
        test_catalog=_catalog(),
    )

    assert plans is not None
    assert plans[0].skill_names == ("frontend-design", "webapp-testing")


def test_plan_assigns_tdd_when_coverage_must_be_generated(monkeypatch):
    monkeypatch.setattr("harness.skills_loader.skill_names", lambda: ["test-driven-development"])
    plans = parse_plan(
        '[{"name":"new behavior","behavior":"add capability",'
        '"acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"result"}],'
        '"depends_on":[]}]',
        test_catalog=_catalog(),
    )

    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"
    assert plans[0].skill_names == ("test-driven-development",)


def test_resume_keeps_evaluator_checkpoint_when_a_future_task_needs_tests(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    current = tasks.create_task("current", "already implemented", verification_spec={"source": "generated"})
    current.status = "in_progress"
    current.verification_state = "passing"
    tasks.save_task(current)
    future = tasks.create_task(
        "future", "wait for current", [current.id], verification_spec={"source": "needs_generation"}
    )
    state = GoalState.new(target="resume", verification="python -m pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.task_plan = [{"name": "current"}, {"name": "future"}]
    state.task_name_ids = {"current": current.id, "future": future.id}
    state.task_ids = [current.id, future.id]
    state.current_task_id = current.id
    state.resume_phase = GoalPhase.EVALUATE.value

    assert runner_mod._resume_target(state) == GoalPhase.EVALUATE.value


def test_resume_re_evaluates_stale_repair_plan_input(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "already implemented", verification_spec={})
    task.status = "in_progress"
    task.verification_state = "passing"
    task.evaluation = {"passed": False, "input_snapshot": "old-snapshot"}
    tasks.save_task(task)
    state = GoalState.new(target="resume", verification="python -m pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.task_plan = [{"name": "current"}]
    state.task_name_ids = {"current": task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    monkeypatch.setattr("harness.verification.snapshot.capture_code_snapshot", lambda _workspace: "new-snapshot")

    assert runner_mod._resume_target(state) == GoalPhase.EVALUATE.value


def test_resume_routes_completed_clean_checkpoint_through_impact_review(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "implemented", verification_spec={})
    task.status = "completed"
    tasks.save_task(task, archived=True)
    (tmp_path / ".tasks" / f"{task.id}.json").unlink()
    state = GoalState.new(target="resume", verification="python -m pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.task_plan = [{"name": "current"}]
    state.task_name_ids = {"current": task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.CLEAN_CHECK.value

    assert runner_mod._resume_target(state) == GoalPhase.IMPACT_REVIEW.value


def test_initialize_recovers_orphan_task_written_before_goal_checkpoint(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    state = GoalState.new(target="recover", verification="python -m pytest -q", workspace=str(tmp_path))
    state.execution_approved = False
    state.task_plan = [{
        "name": "orphan", "behavior": "already persisted",
        "acceptance_cases": [], "verification_spec": {"source": "generated"},
    }]
    orphan = tasks.create_task("orphan", "already persisted", goal_id=state.id, verification_spec={"source": "generated"})
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    GoalRunner(state=state, history=[], context={}, binding=None)._initialize(state)

    assert state.task_ids == [orphan.id]
    assert len(tasks.list_tasks(include_archived=True)) == 1


def test_repair_planning_pauses_after_repeated_task_repairs(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.policy import MAX_REPAIR_ATTEMPTS_PER_TASK

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "still failing", verification_spec={})
    task.status = "in_progress"
    task.repair_history = [{"attempt": index} for index in range(MAX_REPAIR_ATTEMPTS_PER_TASK)]
    tasks.save_task(task)
    state = GoalState.new(target="repair", verification="python -m pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.current_task_id = task.id
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    GoalRunner(state=state, history=[], context={}, binding=None)._repair_plan(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "repair_limit_reached"


def test_test_generation_expands_a_collected_test_file_to_real_node_ids(tmp_path):
    catalog = TestCatalog(
        selectors=(
            "tests/test_generated.py::test_one",
            "tests/test_generated.py::test_many[param]",
        ),
        test_files=("tests/test_generated.py",),
    )

    selectors = GoalRunner._selectors_from_generation(
        '{"test_selectors":["tests/test_generated.py"]}',
        tmp_path,
        catalog=catalog,
    )

    assert selectors == catalog.selectors


def test_test_generation_expands_a_parameterized_test_function(tmp_path):
    catalog = TestCatalog(
        selectors=(
            "tests/test_generated.py::test_many[first]",
            "tests/test_generated.py::test_many[second]",
        ),
        test_files=("tests/test_generated.py",),
    )

    selectors = GoalRunner._selectors_from_generation(
        '{"test_selectors":["tests/test_generated.py::test_many"]}',
        tmp_path,
        catalog=catalog,
    )

    assert selectors == catalog.selectors


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
        lambda **kwargs: calls.append(kwargs["agent_type"]) or '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}',
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False, "stdout": "assert missing behavior", "exit_code": 1, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    bound = load_task(task.id)
    assert calls == ["goal_test_writer"]
    assert state.phase == GoalPhase.SELECT_TASK.value
    assert bound.verification_spec["source"] == "generated"
    assert bound.verification_spec["baseline_result"] == "failing"


def test_test_generation_receives_and_preserves_cross_task_impact_context(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    task.verification_spec["impact_context"] = [{
        "source_task_id": "task_upstream",
        "source_task_subject": "shared permission engine",
        "reason": "approval persistence uses the changed gate",
        "required_coverage": "cover the interaction",
    }]
    tasks.save_task(task)
    prompts = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: prompts.append(kwargs["prompt"]) or '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}',
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False, "stdout": "assert missing behavior", "exit_code": 1, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    bound = tasks.load_task(task.id)
    assert "Cross-Task impact context" in prompts[0]
    assert "approval persistence uses the changed gate" in prompts[0]
    assert bound.verification_spec["impact_context"] == task.verification_spec["impact_context"]


def test_test_generation_rejects_a_passing_baseline(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", lambda **kwargs: '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}')
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": True, "error": None, "timed_out": False, "stdout": "1 passed", "exit_code": 0, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert load_task(task.id).verification_state == "needs_generation"
    assert "passed before implementation" in state.last_error


def test_test_generation_rolls_back_files_when_baseline_is_invalid(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    existing = tests_dir / "test_existing.py"
    existing.write_text("def test_existing(): pass\n", encoding="utf-8")

    def writer(**kwargs):
        (tests_dir / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
    catalogs = iter((
        TestCatalog(selectors=("tests/test_existing.py::test_existing",), test_files=("tests/test_existing.py",)),
        TestCatalog(
            selectors=("tests/test_existing.py::test_existing", "tests/test_new.py::test_new"),
            test_files=("tests/test_existing.py", "tests/test_new.py"),
        ),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": True, "error": None, "timed_out": False, "stdout": "1 passed", "exit_code": 0})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert not (tests_dir / "test_new.py").exists()
    assert existing.read_text(encoding="utf-8") == "def test_existing(): pass\n"


def test_test_generation_rejects_changes_to_existing_fixture_helpers(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    conftest = tests_dir / "conftest.py"
    conftest.write_text("VALUE = 'original'\n", encoding="utf-8")

    def writer(**kwargs):
        conftest.write_text("VALUE = 'rewritten'\n", encoding="utf-8")
        (tests_dir / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert "modified existing test files: tests/conftest.py" in state.last_error
    assert conftest.read_text(encoding="utf-8") == "VALUE = 'original'\n"
    assert not (tests_dir / "test_new.py").exists()
    assert load_task(task.id).verification_state == "needs_generation"


def test_test_generation_rollback_never_expands_nested_test_root_to_source_tree(tmp_path):
    source = tmp_path / "src" / "pkg"
    tests_dir = source / "tests"
    tests_dir.mkdir(parents=True)
    production = source / "module.py"
    production.write_text("VALUE = 1\n", encoding="utf-8")
    existing = tests_dir / "test_existing.py"
    existing.write_text("def test_existing(): pass\n", encoding="utf-8")

    snapshot = GoalRunner._snapshot_test_tree(tmp_path, ("src/pkg/tests",))
    (tests_dir / "test_generated.py").write_text("def test_generated(): pass\n", encoding="utf-8")
    GoalRunner._restore_test_tree(tmp_path, snapshot, ("src/pkg/tests",))

    assert production.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert existing.read_text(encoding="utf-8") == "def test_existing(): pass\n"
    assert not (tests_dir / "test_generated.py").exists()


def test_test_write_roots_do_not_allow_co_located_source_directory():
    catalog = TestCatalog(
        selectors=("src/pkg/test_module.py::test_value",),
        test_files=("src/pkg/test_module.py",),
    )

    roots = GoalRunner._test_write_roots(catalog)

    assert "src/pkg" not in roots


def test_draft_goal_pauses_after_a_failing_test_baseline_for_user_approval(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    state.execution_approved = False
    state.last_error = "old test generation failure"
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", lambda **kwargs: '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}')
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False, "stdout": "assert missing behavior", "exit_code": 1, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert load_task(task.id).verification_spec["baseline_result"] == "failing"
    assert state.phase == GoalPhase.PAUSED.value
    assert state.last_error is None
    assert state.stop_reason == "user_approval_required"


def test_draft_goal_with_existing_tests_still_waits_for_execution_approval(tmp_path, monkeypatch):
    import harness.tasks as tasks
    import harness.goal.runner as runner_mod

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task(
        "existing coverage",
        "behavior",
        verification_spec={"source": "discovered", "command": "python -m pytest -q", "selectors": ["tests/test_x.py::test_x"]},
    )
    state = GoalState.new(target="behavior", verification="python -m pytest -q", workspace=str(tmp_path))
    state.task_plan = [{"name": "existing coverage"}]
    state.task_ids = [task.id]
    state.execution_approved = False
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    GoalRunner(state=state, history=[], context={}, binding=None)._initialize(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "user_approval_required"


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


def test_claim_freezes_hashes_for_discovered_test_bindings(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_bound.py"
    test_file.write_text("def test_bound(): pass\n", encoding="utf-8")
    task = tasks.create_task(
        "bound", "preserve test", verification_spec={
            "source": "discovered",
            "test_files": ["tests/test_bound.py"],
            "selectors": ["tests/test_bound.py::test_bound"],
        },
    )
    state = GoalState.new(target="bound", verification="python -m pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.phase = GoalPhase.CLAIM.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr("harness.verification.snapshot.capture_code_snapshot", lambda workspace: "snapshot")

    GoalRunner(state=state, history=[], context={}, binding=None)._claim(state)

    assert tasks.load_task(task.id).verification_spec["test_hashes"]["tests/test_bound.py"]


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
    monkeypatch.setattr(
        runner,
        "_queue_goal_repair",
        lambda current, detail: runner._apply(current, GoalPhase.REPAIR_PLAN, "goal_regression_requires_repair", error=detail),
    )

    runner._full_verify(state)

    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert state.final_verification["status"] == "failed"
    assert state.final_verification["exit_code"] == 1
    assert state.final_verification["stdout_tail"] == "1 failed"


def test_interrupted_full_verification_pauses_without_creating_a_repair_task(monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.task_ids = ["task_a"]
    state.phase = GoalPhase.FULL_VERIFY.value
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    queued = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod, "reverify_task_command", lambda *args, **kwargs: type("Task", (), {"verification_state": "passing", "last_error": None})())
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "exit_code": 2, "stdout": "KeyboardInterrupt"})(),
    )
    monkeypatch.setattr(
        runner_mod,
        "evidence_from_result",
        lambda *args, **kwargs: type("Evidence", (), {"to_dict": lambda self: {"exit_code": 2, "stdout_tail": "KeyboardInterrupt"}})(),
    )
    monkeypatch.setattr(runner, "_queue_goal_repair", lambda *args: queued.append(args))

    runner._full_verify(state)

    assert queued == []
    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "full_verification_interrupted"
    assert state.resume_phase == GoalPhase.FULL_VERIFY.value
    assert state.final_verification["status"] == "interrupted"


def test_resume_migrates_legacy_interrupted_final_repair(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    repair = tasks.create_task(
        "goal regression repair 6",
        "Repair the Goal-level regression failure: full verification failed with exit code 2",
        verification_spec={"source": "needs_generation"},
    )
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = "paused"
    state.task_ids = ["task_a", repair.id]
    state.task_name_ids = {"first": "task_a", repair.subject: repair.id}
    state.task_plan = [{"name": "first"}, {"name": repair.subject}]
    state.current_task_id = repair.id
    state.resume_phase = GoalPhase.PREPARE_TESTS.value
    state.final_verification = {"status": "failed", "exit_code": 2, "stdout_tail": "KeyboardInterrupt"}

    assert runner_mod._discard_legacy_interrupted_final_repair(state) is True

    migrated = tasks.load_task(repair.id)
    assert migrated.status == "cancelled"
    assert "interrupted" in migrated.last_error.lower()
    assert state.task_ids == ["task_a"]
    assert repair.id not in state.task_name_ids.values()
    assert state.task_plan == [{"name": "first"}]
    assert state.current_task_id is None
    assert state.resume_phase == GoalPhase.FULL_VERIFY.value
    assert state.stop_reason == "full_verification_interrupted"
    assert state.final_verification["status"] == "interrupted"


def test_legacy_interrupted_repair_removes_only_unchanged_generated_test(tmp_path, monkeypatch):
    import hashlib
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    generated = tests_dir / "test_goal_regression.py"
    generated.write_text("def test_generated(): pass\n", encoding="utf-8")
    digest = hashlib.sha256(generated.read_bytes()).hexdigest()
    repair = tasks.create_task(
        "goal regression repair 6",
        "Repair the Goal-level regression failure: full verification failed with exit code 2",
        verification_spec={
            "source": "generated",
            "test_hashes": {"tests/test_goal_regression.py": digest},
        },
    )
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [repair.id]
    state.task_name_ids = {repair.subject: repair.id}
    state.task_plan = [{"name": repair.subject}]
    state.current_task_id = repair.id
    state.final_verification = {"exit_code": 2, "stdout_tail": "KeyboardInterrupt"}

    assert runner_mod._discard_legacy_interrupted_final_repair(state)
    assert not generated.exists()

    # A file changed after the synthetic Task was created is user-owned now;
    # migration must preserve it rather than making cleanup destructive.
    generated.write_text("def test_generated(): assert False\n", encoding="utf-8")
    second = tasks.create_task(
        "goal regression repair 7",
        "Repair the Goal-level regression failure: full verification failed with exit code 2",
        verification_spec={
            "source": "generated",
            "test_hashes": {"tests/test_goal_regression.py": digest},
        },
    )
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [second.id]
    state.task_name_ids = {second.subject: second.id}
    state.task_plan = [{"name": second.subject}]
    state.current_task_id = second.id
    state.final_verification = {"exit_code": 2, "stdout_tail": "KeyboardInterrupt"}

    assert runner_mod._discard_legacy_interrupted_final_repair(state)
    assert generated.exists()


def test_resume_does_not_migrate_before_acquiring_goal_lease(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = "paused"
    called = []
    monkeypatch.setattr(runner_mod, "load_goal", lambda: state)
    monkeypatch.setattr(runner_mod, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(runner_mod, "_discard_legacy_interrupted_final_repair", lambda current: called.append(current))
    monkeypatch.setattr(
        runner_mod,
        "acquire_goal_lease",
        lambda current: (_ for _ in ()).throw(runner_mod.GoalLeaseError("already active")),
    )

    try:
        runner_mod.resume_goal(history=[], context={}, binding=None)
    except runner_mod.GoalBusyError:
        pass
    else:
        raise AssertionError("expected an active Goal lease to block resume")

    assert called == []


def test_cancel_can_finalize_a_paused_goal_without_a_live_runner(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = "paused"
    monkeypatch.setattr(runner_mod, "load_goal", lambda: state)
    monkeypatch.setattr(runner_mod, "acquire_goal_lease", lambda current: "lease")
    monkeypatch.setattr(runner_mod, "release_goal_lease", lambda *args: None)
    monkeypatch.setattr(runner_mod, "save_goal", lambda current: None)
    monkeypatch.setattr(runner_mod, "archive_goal", lambda current: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    cancelled = runner_mod.cancel_goal()

    assert cancelled.status == "cancelled"
    assert cancelled.phase == GoalPhase.CANCELLED.value
    assert cancelled.stop_reason == "cancelled_by_user"


def test_resume_migrates_untouched_unbound_goal_regression_repair(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    repair = tasks.create_task(
        "goal regression repair 6",
        "Repair the Goal-level regression failure: full verification failed with exit code 1",
        verification_spec={"source": "needs_generation"},
    )
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = ["task_a", repair.id]
    state.task_name_ids = {"first": "task_a", repair.subject: repair.id}
    state.task_plan = [{"name": "first"}, {"name": repair.subject}]
    state.current_task_id = repair.id

    assert runner_mod._discard_unbound_goal_regression_repair(state)
    assert tasks.load_task(repair.id).status == "cancelled"
    assert state.task_ids == ["task_a"]
    assert state.current_task_id is None
    assert state.resume_phase == GoalPhase.FULL_VERIFY.value


def test_goal_regression_reopens_the_existing_task_that_owns_the_selector(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import GoalRegressionDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    test_file = tmp_path / "tests" / "test_web.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_browser(): assert False\n", encoding="utf-8")
    owner = tasks.create_task(
        "sandbox permissions",
        "deliver sandbox permissions",
        verification_spec={
            "source": "discovered",
            "selectors": ["tests/test_web.py::test_browser"],
            "test_files": ["tests/test_web.py"],
        },
    )
    state = GoalState.new(target="x", verification="python -m pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.FULL_VERIFY.value
    state.task_ids = [owner.id]
    state.final_verification = {
        "stdout_tail": "FAILED tests/test_web.py::test_browser - assertion failed",
        "exit_code": 1,
    }
    monkeypatch.setattr(runner_mod, "save_goal", lambda current: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_goal_regression_repair",
        lambda *args, **kwargs: GoalRegressionDecision(
            "reopen_existing", owner.id, "restore the broken sandbox behavior", "reopen owner"
        ),
    )
    GoalRunner(state=state, history=[], context={}, binding=None)._queue_goal_repair(
        state, "full verification failed with exit code 1"
    )

    assert state.current_task_id == owner.id
    assert state.task_ids == [owner.id]
    assert tasks.load_task(owner.id).last_error.startswith("Final verification failed")


def test_goal_regression_analysis_can_create_a_bound_sixth_repair_task(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import GoalRegressionDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    state = GoalState.new(target="x", verification="python -m pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.FULL_VERIFY.value
    state.final_verification = {
        "stdout_tail": "FAILED tests/test_web.py::test_browser - assertion failed",
        "exit_code": 1,
    }
    monkeypatch.setattr(runner_mod, "save_goal", lambda current: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_goal_regression_repair",
        lambda *args, **kwargs: GoalRegressionDecision(
            "create_repair_task", None, "restore the intentionally changed browser permission behavior", "new bounded repair"
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "collect_pytest_catalog",
        lambda root: TestCatalog(
            selectors=("tests/test_web.py::test_browser",),
            test_files=("tests/test_web.py",),
        ),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._queue_goal_repair(
        state, "full verification failed with exit code 1"
    )

    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert len(state.task_ids) == 1
    repair = tasks.load_task(state.current_task_id)
    assert repair.subject == "goal regression repair 1"
    assert repair.verification_spec["selectors"] == ["tests/test_web.py::test_browser"]
    assert repair.verification_spec["source"] == "discovered"


def test_replan_returns_current_task_to_additive_test_preparation(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import RepairDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task(
        "repair me",
        "deliver the requested behavior",
        goal_id="goal_demo",
        acceptance_cases=[{"id": "AC1", "given": "x", "when": "y", "then": "z"}],
        verification_spec={"source": "generated", "selectors": ["tests/test_x.py::test_x"]},
        evaluation_required=True,
    )
    tasks.claim_task(task.id)
    tasks.record_task_evaluation(task.id, {"passed": False, "route": "replan", "summary": "coverage must be revised"})
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.id = "goal_demo"
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.phase = GoalPhase.REPAIR_PLAN.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_task_repair",
        lambda *args, **kwargs: RepairDecision("replan", "add an integration case", summary="replan tests"),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._repair_plan(state)

    repaired = tasks.load_task(task.id)
    assert state.phase == GoalPhase.PREPARE_TESTS.value
    assert repaired.verification_state == "needs_generation"
    assert repaired.verification_spec["allow_posthoc_test"] is True
    assert repaired.repair_history[-1]["action"] == "replan"


def test_repair_plan_json_format_failure_is_not_marked_provider_unavailable(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import RepairDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("repair", "behavior", verification_spec={})
    tasks.claim_task(task.id)
    tasks.record_task_evaluation(task.id, {"passed": False, "route": "implementation_fix"})
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.REPAIR_PLAN.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_task_repair",
        lambda *args, **kwargs: RepairDecision("blocked", "", error="repair planner returned no JSON", unavailable=True),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._repair_plan(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "repair_plan_format_error"


def test_goal_worker_context_excludes_stale_cli_state():
    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    runner = GoalRunner(
        state=state,
        history=[{"role": "user", "content": "old request"}],
        context={
            "project_instructions": "follow local rules",
            "memories": ["known fact"],
            "connected_mcp": {},
            "latest_user_query": "unrelated old request",
            "todos": [{"content": "stale todo"}],
            "writing_mode": True,
        },
        binding=None,
    )

    assert runner._goal_worker_context() == {
        "project_instructions": "follow local rules",
        "memories": ["known fact"],
        "connected_mcp": {},
    }


def test_worker_round_limit_creates_a_durable_rollover(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("long task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="long task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.ACT.value
    state.worker_round_limit = 2
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    def capped_worker(*args, stats, **kwargs):
        stats.llm_rounds = 2
        stats.stop_reason = "max_rounds"
        return "worker stopped after max rounds"

    monkeypatch.setattr(runner_mod, "run_agent_task", capped_worker)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    runner._act(state)

    assert state.phase == GoalPhase.ROLLOVER.value
    assert state.status == "running"
    assert state.worker_generation == 1
    assert state.worker_rollovers == 1
    runner._rollover(state)
    assert state.phase == GoalPhase.VERIFY.value


def test_no_progress_routes_to_repair_instead_of_failing(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("stuck task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="stuck task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.ACT.value
    state.no_progress_count = 1
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", lambda *args, stats, **kwargs: "no changes")
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(runner, "_progress_snapshot", lambda state: ("unchanged",))

    runner._act(state)

    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert state.status == "running"
    assert "no observable progress" in state.last_error


def test_provider_error_pauses_without_consuming_repair_budget(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("network task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="network task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.ACT.value
    state.no_progress_count = 1
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    def unavailable(*args, stats, **kwargs):
        stats.stop_reason = "provider_error"
        return "[goal_worker] failed: APIConnectionError: Connection error"

    monkeypatch.setattr(runner_mod, "run_agent_task", unavailable)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(runner, "_progress_snapshot", lambda state: ("unchanged",))

    runner._act(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "provider_unavailable"
    assert state.repair_attempts == 0
    assert "APIConnectionError" in state.last_error


def test_permission_request_pauses_goal_instead_of_failing(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("permission task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="permission task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.ACT.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(runner_mod, "goal_permission_pending", lambda: True)
    monkeypatch.setattr(runner_mod, "run_agent_task", lambda **kwargs: "work completed")

    GoalRunner(state=state, history=[], context={}, binding=None)._act(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.status == "paused"
    assert state.stop_reason == "permission_wait"
    assert "required permission" in state.last_error


def test_invalid_evaluator_output_pauses_for_a_retry(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("review task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="review task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.EVALUATE.value
    task.evaluation_required = True
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(
        runner_mod,
        "run_task_evaluation",
        lambda *args, **kwargs: type(
            "Task",
            (),
            {"evaluation": {"passed": None, "error": "no JSON object found in evaluator output"}},
        )(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._evaluate(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "evaluation_unavailable"
    assert "no JSON object" in state.last_error


def test_act_uses_isolated_goal_worker_with_task_prompt(tmp_path, monkeypatch):
    import harness.agents.registry as registry_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("implement task", "change only this behavior", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="deliver behavior", verification="python -m pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.ACT.value
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(registry_mod, "validate_agent_model", lambda agent_type: None)

    def worker(**kwargs):
        calls.append(kwargs)
        return "implemented"

    monkeypatch.setattr(runner_mod, "run_agent_task", worker)
    runner = GoalRunner(
        state=state,
        history=[],
        context={"project_instructions": "Run focused tests before stopping."},
        binding=None,
    )

    runner._act(state)

    assert not hasattr(runner_mod, "agent_loop")
    assert len(calls) == 1
    assert calls[0]["agent_type"] == "goal_worker"
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["max_rounds"] == state.worker_round_limit
    assert "Work only on this Task" in calls[0]["prompt"]
    assert "Run focused tests before stopping." in calls[0]["prompt"]


def test_goal_event_snapshot_exposes_task_contract_for_terminal_ui(tmp_path, monkeypatch):
    from harness.goal.runner import goal_event_payload

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    state.phase = GoalPhase.VERIFY.value
    payload = goal_event_payload(state)

    assert payload["current_task_id"] == task.id
    assert payload["verification"] == "pytest -q"
    assert payload["stop_reason"] is None
    assert payload["final_verification"] is None
    assert payload["resume_phase"] is None
    assert payload["execution_approved"] is state.execution_approved
    assert payload["task_cycles"] == state.attempts
    assert payload["tasks"] == [
        {
            "id": task.id,
            "subject": "new behavior",
            "status": "pending",
            "verification_state": "needs_generation",
                "blocked_by": [],
                "acceptance_cases": task.acceptance_cases,
                "skills": [],
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
