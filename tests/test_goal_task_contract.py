"""Contracts for the latest Goal -> Task -> evidence model."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from harness.goal.planner import TaskPlan, discovery_readiness_error, parse_plan
from harness.goal.models import GoalPhase, GoalState
from harness.goal.runner import GoalRunner
from harness.verification.catalog import TestCatalog
from harness.verification.node_adapter import NodeTestCatalog


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


def test_invalid_existing_case_mapping_falls_back_to_test_generation():
    raw = ('[{"name":"pages","behavior":"all pages return",'
           '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
           '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[],'
           '"case_selectors":{"AC2":["tests/test_api.py::test_lists_all_pages"]}}]')
    plans = parse_plan(raw, test_catalog=_catalog())
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"


def test_partial_existing_test_mapping_falls_back_to_test_generation():
    raw = ('[{"name":"pages","behavior":"all pages return",'
           '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"},'
           '{"id":"AC2","given":"an empty page","when":"listed","then":"an empty response"}],'
           '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[], '
           '"case_selectors":{"AC1":["tests/test_api.py::test_lists_all_pages"]}}]')

    plans = parse_plan(raw, test_catalog=_catalog())

    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"
    assert not plans[0].verification_spec.selectors
    assert not plans[0].verification_spec.case_selectors


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


def test_worker_scope_gate_normalizes_nested_execution_workspace_paths(monkeypatch, tmp_path):
    from harness.goal import runner as runner_mod
    from harness.tasks import Task

    execution = tmp_path / "node_tui"
    execution.mkdir()
    state = GoalState.new(target="x", verification="npm test", workspace=str(tmp_path))
    state.execution_workspace = str(execution)
    task = Task(
        id="task_nested_scope", subject="x", description="x", status="in_progress", owner="goal:x",
        blockedBy=[], scope_paths=["src-open/interaction.ts"],
    )
    monkeypatch.setattr("harness.verification.snapshot.capture_dirty_file_hashes", lambda _workspace: {
        "node_tui/src-open/interaction.ts": "new",
    })

    assert runner_mod.GoalRunner._validate_task_scope(state, task) is None


def test_worker_scope_gate_allows_task_bound_generated_test_files(monkeypatch, tmp_path):
    from harness.goal import runner as runner_mod
    from harness.tasks import Task

    state = GoalState.new(target="x", verification="bun test", workspace=str(tmp_path))
    task = Task(
        id="task_test_scope", subject="x", description="x", status="in_progress", owner="goal:x",
        blockedBy=[], scope_paths=["src"],
        verification_spec={"test_files": ["test/footer-state.test.ts"]},
    )
    monkeypatch.setattr(
        "harness.verification.snapshot.capture_dirty_file_hashes",
        lambda _workspace: {
            "src/app.ts": "new",
            "test/footer-state.test.ts": "new",
            "test/message-queue.test.ts": "preexisting-test",
        },
    )

    assert runner_mod.GoalRunner._validate_task_scope(state, task) is None


def test_keybinding_task_requires_a_pure_logic_test_boundary():
    from harness.tasks import Task

    task = Task(
        id="task_keys", subject="cursor and character keybindings", description="move the cursor and edit input",
        status="pending", owner=None, blockedBy=[],
    )

    assert GoalRunner._requires_pure_logic_test(task) is True


def test_pure_logic_test_cannot_import_a_complete_app_entry(tmp_path):
    from harness.tasks import Task

    test_file = tmp_path / "test" / "composer.test.ts"
    test_file.parent.mkdir()
    test_file.write_text("import * as App from '../src-open/App.tsx';\n", encoding="utf-8")
    task = Task(
        id="task_keys", subject="cursor and character keybindings", description="move the cursor and edit input",
        status="pending", owner=None, blockedBy=[],
    )

    error = GoalRunner._generated_test_architecture_error(tmp_path, task, ("test/composer.test.ts",))

    assert error is not None
    assert "complete application entry" in error


def test_nested_workspace_dirty_hashes_ignore_sibling_repository_files(monkeypatch, tmp_path):
    from harness.verification import snapshot

    execution = tmp_path / "node_tui"
    execution.mkdir()
    (execution / "src-open").mkdir()
    (execution / "src-open" / "interaction.ts").write_text("changed", encoding="utf-8")
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "goal.py").write_text("unrelated", encoding="utf-8")

    def fake_git_bytes(*args, cwd):
        if args[:2] == ("diff", "--name-only"):
            return b"node_tui/src-open/interaction.ts\0harness/goal.py\0"
        return b""

    monkeypatch.setattr(snapshot, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(snapshot, "_git", lambda *args, cwd: str(tmp_path) + "\n")

    hashes = snapshot.capture_dirty_file_hashes(execution)

    assert set(hashes) == {"src-open/interaction.ts"}


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


def test_plan_rejects_a_code_task_grounded_only_in_requirement_text():
    manifest = {
        "repo_files": ["docs/requirements.md", "src/app.ts"],
        "evidence": [
            {"id": "E1", "path": "docs/requirements.md"},
        ],
    }
    raw = ('[{"name":"queue","behavior":"queue messages",'
           '"acceptance_cases":[{"id":"AC1","given":"running","when":"sent","then":"queued"}],'
           '"depends_on":[],"scope_paths":["src/app.ts"],"evidence_refs":["E1"],'
           '"test_strategy":"new focused test"}]')

    assert parse_plan(raw, test_catalog=_catalog(), discovery_manifest=manifest) is None


def test_plan_derives_source_evidence_refs_from_a_code_scope():
    manifest = {
        "repo_files": ["docs/requirements.md", "src/app.ts"],
        "evidence": [
            {"id": "E1", "path": "docs/requirements.md"},
            {"id": "E2", "path": "src/app.ts"},
        ],
    }
    raw = ('[{"name":"queue","behavior":"queue messages",'
           '"acceptance_cases":[{"id":"AC1","given":"running","when":"sent","then":"queued"}],'
           '"depends_on":[],"scope_paths":["src/app.ts"],"evidence_refs":["E1"],'
           '"test_strategy":"generated focused test"}]')

    plans = parse_plan(raw, test_catalog=_catalog(), discovery_manifest=manifest)

    assert plans is not None
    assert plans[0].evidence_refs == ("E1", "E2")


def test_parse_plan_reports_all_task_contract_errors():
    manifest = {"repo_files": ["src/app.ts"], "evidence": [{"id": "E1", "path": "src/app.ts"}]}
    raw = ('[{"name":"one","behavior":"first",'
           '"acceptance_cases":[{"id":"AC1","given":"x","when":"y","then":"z"}],"depends_on":[]},'
           '{"name":"two","behavior":"second",'
           '"acceptance_cases":[{"id":"AC1","given":"x","when":"y","then":"z"}],'
           '"depends_on":[],"scope_paths":["outside"],"test_strategy":"focused"}]')

    from harness.goal.planner import _parse_plan_result
    plans, error = _parse_plan_result(raw, test_catalog=_catalog(), discovery_manifest=manifest)

    assert plans is None
    assert error is not None
    assert "Task 1 (one) is missing scope_paths" in error
    assert "Task 1 (one) is missing test_strategy" in error
    assert "Task 2 (two) scope_paths must be discovered workspace files or directories" in error


def test_discovery_readiness_ignores_agent_gap_prose_when_source_evidence_exists():
    manifest = {
        "evidence": [
            {"id": "E1", "path": "src/autocomplete.ts"},
            {"id": "E2", "path": "src-open/App.tsx"},
        ],
        "gaps": [
            "src/autocomplete.ts not in assigned files - cannot verify current completion features",
            "src-open/App.tsx not in assigned files - cannot assess current interaction behavior",
        ],
    }

    assert discovery_readiness_error(manifest) is None


def test_discovery_readiness_does_not_treat_assigned_paths_as_read_evidence():
    manifest = {
        "repo_files": ["docs/requirements.md", "src/autocomplete.ts"],
        "evidence": [{"id": "E1", "path": "docs/requirements.md"}],
        "jobs": [{"status": "done", "read_paths": ["src/autocomplete.ts"]}],
    }

    assert discovery_readiness_error(manifest) == "Discovery produced no validated source-code evidence. Continue discovery before planning."


def test_discovery_readiness_rejects_uncovered_source_context():
    manifest = {
        "repo_files": ["docs/requirements.md", "src/autocomplete.ts"],
        "evidence": [{"id": "E1", "path": "docs/requirements.md"}],
    }

    error = discovery_readiness_error(manifest)

    assert error is not None
    assert "source-code evidence" in error


def test_discovery_readiness_ignores_external_and_generic_role_gap_prose():
    manifest = {
        "repo_files": ["docs/requirements.md", "src/app.ts"],
        "evidence": [{"id": "E1", "path": "src/app.ts"}],
        "gaps": [
            "dsh-TUI reference files (lib/types/components/PromptInput.js) are not assigned",
            "No assigned source file contains current TUI interaction logic",
        ],
    }

    assert discovery_readiness_error(manifest) is None


def test_empty_existing_case_mapping_falls_back_to_test_generation():
    raw = ('[{"name":"pages","behavior":"all pages return",'
           '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
           '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[],'
           '"case_selectors":{"AC1":[]}}]')

    plans = parse_plan(raw, test_catalog=_catalog())
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"


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


def test_resume_retries_paused_impact_review_for_completed_task(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("completed", "implemented", verification_spec={})
    task.status = "completed"
    tasks.save_task(task, archived=True)
    (tmp_path / ".tasks" / f"{task.id}.json").unlink()
    state = GoalState.new(target="resume", verification="python -m pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.task_plan = [{"name": "completed"}]
    state.task_name_ids = {"completed": task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.IMPACT_REVIEW.value

    assert runner_mod._resume_target(state) == GoalPhase.IMPACT_REVIEW.value


def test_resume_retries_paused_repair_plan_for_pending_task(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("repair", "needs a repair decision", verification_spec={})
    state = GoalState.new(target="resume", verification="python -m pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.task_plan = [{"name": "repair"}]
    state.task_name_ids = {"repair": task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.REPAIR_PLAN.value

    assert runner_mod._resume_target(state) == GoalPhase.REPAIR_PLAN.value


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


def test_initialize_persists_every_task_in_a_large_plan(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    state = GoalState.new(target="large goal", verification="python -m pytest -q", workspace=str(tmp_path))
    state.execution_approved = False
    state.task_plan = [
        {
            "name": f"task {index + 1}",
            "behavior": f"deliver behavior {index + 1}",
            "depends_on": [f"task {index}"] if index else [],
            "acceptance_cases": [{
                "id": "AC1",
                "given": "the preceding task is complete",
                "when": f"behavior {index + 1} is used",
                "then": f"deliverable {index + 1} is observable",
            }],
            "verification_spec": {"source": "generated"},
        }
        for index in range(20)
    ]
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)

    GoalRunner(state=state, history=[], context={}, binding=None)._initialize(state)

    persisted = tasks.list_tasks()
    assert len(state.task_ids) == 20
    assert len(state.task_name_ids) == 20
    assert len(persisted) == 20
    assert len({task.id for task in persisted}) == 20
    assert all(len(task.id.rsplit("_", 1)[-1]) == 12 for task in persisted)
    final_task = tasks.load_task(state.task_name_ids["task 20"])
    assert final_task.blockedBy == [state.task_name_ids["task 19"]]


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


def test_resume_starts_a_new_bounded_repair_epoch_after_limit(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "still failing", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.execution_approved = True
    state.task_plan = [{"name": task.subject, "scope_paths": []}]
    state.task_name_ids = {task.subject: task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    state.stop_reason = "repair_limit_reached"
    state.repair_attempts = 4
    state.no_progress_count = 3

    target = runner_mod._resume_target(state)

    assert target == GoalPhase.ACT.value
    assert state.repair_attempts == 0
    assert state.repair_epoch == 1
    assert state.no_progress_count == 0
    assert "new bounded repair epoch" not in state.last_error
    assert "verify the current Task" in state.last_error


def test_resume_verifies_a_workspace_change_before_starting_another_worker(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "changed after pause", verification_spec={})
    tasks.claim_task(task.id)
    task = tasks.load_task(task.id)
    task.start_snapshot = "before"
    tasks.save_task(task)
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.execution_approved = True
    state.task_plan = [{"name": task.subject, "scope_paths": []}]
    state.task_name_ids = {task.subject: task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    state.stop_reason = "repair_limit_reached"
    monkeypatch.setattr("harness.verification.snapshot.capture_code_snapshot", lambda _workspace: "after")

    assert runner_mod._resume_target(state) == GoalPhase.VERIFY.value
    assert state.repair_epoch == 1
    assert "Workspace changed" in state.last_error


def test_resume_skips_implementation_when_task_verification_and_evaluation_pass(tmp_path, monkeypatch):
    import harness.tasks as tasks
    import harness.goal.runner as runner_mod

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task(
        "already verified", "keep the verified behavior", verification_spec={"source": "generated"},
        evaluation_required=True,
    )
    tasks.claim_task(task.id)
    task = tasks.load_task(task.id)
    task.verification_state = "passing"
    task.evaluation = {"passed": True}
    tasks.save_task(task)
    state = GoalState.new(target="resume", verification="pytest -q", workspace=str(tmp_path), evaluation_required=True)
    state.initialization_complete = True
    state.execution_approved = True
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.resume_phase = GoalPhase.ACT.value

    assert runner_mod._resume_target(state) == GoalPhase.CLEAN_CHECK.value


def test_new_repair_epoch_does_not_count_legacy_task_history(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import RepairDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("current", "still failing", verification_spec={})
    tasks.claim_task(task.id)
    task = tasks.load_task(task.id)
    task.repair_history = [{"attempt": index} for index in range(4)]
    tasks.save_task(task)
    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    state.initialization_complete = True
    state.execution_approved = True
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.repair_epoch = 1
    state.repair_attempts = 0
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_task_repair",
        lambda *args, **kwargs: RepairDecision("implementation_fix", "apply the verified fix"),
    )

    runner_mod.GoalRunner(state=state, history=[], context={}, binding=None)._repair_plan(state)

    assert state.phase == GoalPhase.ACT.value
    repaired = tasks.load_task(task.id)
    assert repaired.repair_history[-1]["repair_epoch"] == 1


def test_repair_planner_corrects_invalid_json_once_before_falling_back(tmp_path):
    from harness.agents.runner import AgentTaskStats
    from harness.goal.repair import plan_task_repair
    from harness.tasks import Task

    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    task = Task(
        id="task_repair",
        subject="repair",
        description="restore behavior",
        status="in_progress",
        owner="goal:test",
        blockedBy=[],
    )
    replies = iter(("I would change the adapter.", '{"action":"implementation_fix","instructions":"Repair the adapter and rerun the bound test."}'))

    def runner(**kwargs):
        kwargs["stats"].llm_rounds = 1
        return next(replies)

    stats = AgentTaskStats()
    decision = plan_task_repair(
        state,
        task,
        {"passed": False, "summary": "bound test failed"},
        cwd=str(tmp_path),
        stats=stats,
        runner=runner,
    )

    assert decision.action == "implementation_fix"
    assert not decision.unavailable
    assert stats.llm_rounds == 2


def test_repair_planner_uses_deterministic_fallback_after_two_invalid_json_replies(tmp_path):
    from harness.agents.runner import AgentTaskStats
    from harness.goal.repair import plan_task_repair
    from harness.tasks import Task

    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    task = Task(
        id="task_repair",
        subject="repair",
        description="restore behavior",
        status="in_progress",
        owner="goal:test",
        blockedBy=[],
    )
    replies = iter(("plain text", "still not JSON"))

    def runner(**kwargs):
        kwargs["stats"].llm_rounds = 1
        return next(replies)

    decision = plan_task_repair(
        state,
        task,
        {"passed": False, "summary": "bound test failed"},
        cwd=str(tmp_path),
        stats=AgentTaskStats(),
        runner=runner,
    )

    assert decision.action == "implementation_fix"
    assert decision.format_fallback
    assert not decision.unavailable
    assert "Do not regenerate" in decision.instructions
    assert "after JSON correction" in (decision.error or "")


def test_repair_planner_preserves_provider_failure_over_missing_json(tmp_path):
    from harness.agents.runner import AgentTaskStats
    from harness.goal.repair import plan_task_repair
    from harness.tasks import Task

    state = GoalState.new(target="repair", verification="pytest -q", workspace=str(tmp_path))
    task = Task(
        id="task_repair",
        subject="repair",
        description="restore behavior",
        status="in_progress",
        owner="goal:test",
        blockedBy=[],
    )

    def unavailable(**kwargs):
        kwargs["stats"].stop_reason = "provider_error"
        return "provider unavailable"

    decision = plan_task_repair(
        state,
        task,
        {"passed": False},
        cwd=str(tmp_path),
        stats=AgentTaskStats(),
        runner=unavailable,
    )

    assert decision.unavailable
    assert decision.error == "repair planner stopped: provider_error"


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


def test_test_generation_keeps_multiple_source_grounded_test_designs():
    raw = json.dumps({
        "test_design": [
            {"layer": "pure_logic", "target": "src/completion.ts", "seam": "requestVersion", "cases": ["CM1", "CM2"], "runner": "bun test"},
            {"layer": "terminal_integration", "target": "src-open/App.tsx", "seam": "completion menu", "cases": ["CM3"], "runner": "bun test"},
        ],
    })

    design = GoalRunner._test_design_from_generation(raw)

    assert [item["layer"] for item in design] == ["pure_logic", "terminal_integration"]
    assert design[0]["cases"] == ["CM1", "CM2"]


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


def _needs_generation_node_task(tmp_path, monkeypatch):
    import harness.tasks as tasks

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    task.verification_spec["adapter"] = "node"
    tasks.save_task(task)
    state.verification = "node --import tsx --test"
    return task, state


def _patch_node_catalogs(monkeypatch, *catalogs):
    from harness.verification.node_adapter import NodeTestAdapter

    discovered = iter(catalogs)
    monkeypatch.setattr(NodeTestAdapter, "discover", lambda self, context: next(discovered))


def _failing_node_baseline(command="node --import tsx --test test/new.test.ts"):
    return type("Result", (), {
        "passed": False,
        "error": None,
        "timed_out": False,
        "stdout": "assert missing behavior",
        "exit_code": 1,
        "duration_ms": 5,
        "command": command,
    })()


def test_node_test_generation_prompt_uses_node_conventions(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_node_task(tmp_path, monkeypatch)
    prompts = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: prompts.append(kwargs["prompt"]) or (
            '{"test_selectors":["test/new.test.ts::new behavior"],'
            '"case_selectors":{"AC1":["test/new.test.ts::new behavior"]}}'
        ),
    )
    _patch_node_catalogs(
        monkeypatch,
        NodeTestCatalog((), ()),
        NodeTestCatalog(("test/new.test.ts::new behavior",), ("test/new.test.ts",)),
    )
    monkeypatch.setattr(runner_mod, "run_verification", lambda *args, **kwargs: _failing_node_baseline())

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert "test/example.test.ts::behavior name" in prompts[0]
    assert "test/**/*.test.ts" in prompts[0]
    assert "test/**/*.test.tsx" in prompts[0]
    assert "Do not put JSX in a .test.ts file" in prompts[0]
    assert "excludes dependency trees such as node_modules" in prompts[0]
    assert "empty selector list is not a valid result" in prompts[0]
    assert ".py" not in prompts[0]
    assert load_task(task.id).verification_spec["adapter"] == "node"


def test_node_test_generation_repairs_wrong_model_selector_from_machine_catalog(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_node_task(tmp_path, monkeypatch)
    calls = []
    actual_selector = "test/new.test.ts::actual behavior"
    wrong_selector = "test/new.test.ts::invented behavior"

    def writer(**kwargs):
        calls.append(kwargs)
        selector = wrong_selector if len(calls) == 1 else actual_selector
        return json.dumps({
            "test_selectors": [selector],
            "case_selectors": {"AC1": [selector]},
        })

    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
    _patch_node_catalogs(
        monkeypatch,
        NodeTestCatalog((), ()),
        NodeTestCatalog((actual_selector,), ("test/new.test.ts",)),
    )
    monkeypatch.setattr(runner_mod, "run_verification", lambda *args, **kwargs: _failing_node_baseline())

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    bound = load_task(task.id)
    assert len(calls) == 2
    assert calls[1]["tools_override"] == ()
    assert actual_selector in calls[1]["prompt"]
    assert wrong_selector not in calls[1]["prompt"]
    assert bound.verification_spec["selectors"] == [actual_selector]
    assert state.phase == GoalPhase.SELECT_TASK.value


def test_test_generation_repairs_incomplete_acceptance_case_mapping(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_node_task(tmp_path, monkeypatch)
    calls = []
    selector = "test/new.test.ts::new behavior"

    def writer(**kwargs):
        calls.append(kwargs)
        mapping = {} if len(calls) == 1 else {"AC1": [selector]}
        return json.dumps({"test_selectors": [selector], "case_selectors": mapping})

    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
    _patch_node_catalogs(
        monkeypatch,
        NodeTestCatalog((), ()),
        NodeTestCatalog((selector,), ("test/new.test.ts",)),
    )
    monkeypatch.setattr(runner_mod, "run_verification", lambda *args, **kwargs: _failing_node_baseline())

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert len(calls) == 2
    assert calls[1]["tools_override"] == ()
    assert load_task(task.id).verification_spec["case_selectors"] == {"AC1": [selector]}


def test_test_generation_failed_selector_repair_persists_node_diagnostic(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    _task, state = _needs_generation_node_task(tmp_path, monkeypatch)
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    generated_file = test_dir / "new.test.ts"
    actual_selector = "test/new.test.ts::actual behavior"
    returned_selectors = (
        "test/new.test.ts::invented behavior",
        "test/other.test.ts::still invented",
    )
    calls = []

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            generated_file.write_text("test('actual behavior', () => {});\n", encoding="utf-8")
        selector = returned_selectors[len(calls) - 1]
        return json.dumps({
            "test_selectors": [selector],
            "case_selectors": {"AC1": [selector]},
        })

    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
    _patch_node_catalogs(
        monkeypatch,
        NodeTestCatalog((), ()),
        NodeTestCatalog((actual_selector,), ("test/new.test.ts",)),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    diagnostic = json.loads(state.last_error.split("Diagnostic: ", 1)[1])
    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "test_generation_required"
    assert diagnostic["adapter"] == "node"
    assert diagnostic["generated_selectors"] == [actual_selector]
    assert diagnostic["requested_selectors"] == [returned_selectors[1]]
    assert "did not resolve in the node catalog" in diagnostic["mismatch"]
    assert returned_selectors[1] in diagnostic["response_tail"]
    assert not generated_file.exists()


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


def test_test_generation_accepts_import_error_for_its_planned_new_module(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    task.planned_new = ["harness/goal/sandbox.py"]
    state.last_error = "prior baseline failed"
    tasks.save_task(task)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}',
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {
            "passed": False, "error": None, "timed_out": False,
            "stdout": "ERROR collecting tests/test_new.py\nModuleNotFoundError: No module named 'harness.goal.sandbox'",
            "exit_code": 2, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new",
        })(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.SELECT_TASK.value
    assert state.last_error is None
    assert tasks.load_task(task.id).verification_spec["baseline_result"] == "failing"


def test_test_generation_rejects_unrelated_import_error_baseline(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    task.planned_new = ["harness/goal/sandbox.py"]
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}',
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_new.py::test_new",), test_files=("tests/test_new.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {
            "passed": False, "error": None, "timed_out": False,
            "stdout": "ERROR collecting tests/test_new.py\nModuleNotFoundError: No module named 'unrelated.module'",
            "exit_code": 2, "duration_ms": 5, "command": "pytest -q tests/test_new.py::test_new",
        })(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "test_generation_required"
    assert "unrelated.module" in state.last_error


def test_test_generation_binds_only_the_current_runnable_task(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    first = tasks.create_task(
        "first", "first missing behavior",
        acceptance_cases=[{"id": "AC1", "given": "x", "when": "y", "then": "z"}],
        verification_spec={"source": "needs_generation"},
    )
    second = tasks.create_task(
        "second", "future missing behavior",
        acceptance_cases=[{"id": "AC1", "given": "x", "when": "y", "then": "z"}],
        verification_spec={"source": "needs_generation"},
    )
    state = GoalState.new(target="lazy tests", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [first.id, second.id]
    state.current_task_id = first.id
    state.phase = GoalPhase.PREPARE_TESTS.value
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: calls.append(kwargs) or (
            '{"test_selectors":["tests/test_first.py::test_first"],'
            '"case_selectors":{"AC1":["tests/test_first.py::test_first"]}}'
        ),
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_first.py::test_first",), test_files=("tests/test_first.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False, "stdout": "assert missing behavior", "exit_code": 1, "duration_ms": 5, "command": "pytest -q tests/test_first.py::test_first"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert len(calls) == 1
    assert tasks.load_task(first.id).verification_spec["source"] == "generated"
    assert tasks.load_task(second.id).verification_state == "needs_generation"
    assert state.current_task_id == first.id


def test_test_generation_without_a_current_task_persists_the_actual_task(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    bound = tasks.create_task("bound", "already covered", verification_spec={"source": "generated"})
    target = tasks.create_task(
        "target", "needs coverage",
        acceptance_cases=[{"id": "AC1", "given": "x", "when": "y", "then": "z"}],
        verification_spec={"source": "needs_generation"},
    )
    state = GoalState.new(target="identity", verification="pytest -q", workspace=str(tmp_path))
    state.task_ids = [bound.id, target.id]
    state.current_task_id = None
    state.phase = GoalPhase.PREPARE_TESTS.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(
        runner_mod,
        "run_agent_task",
        lambda **kwargs: '{"test_selectors":["tests/test_target.py::test_target"],"case_selectors":{"AC1":["tests/test_target.py::test_target"]}}',
    )
    catalogs = iter((
        TestCatalog(),
        TestCatalog(selectors=("tests/test_target.py::test_target",), test_files=("tests/test_target.py",)),
    ))
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: next(catalogs))
    monkeypatch.setattr(
        runner_mod,
        "run_verification",
        lambda *args, **kwargs: type("Result", (), {"passed": False, "error": None, "timed_out": False, "stdout": "assert missing behavior", "exit_code": 1, "duration_ms": 5, "command": "pytest -q tests/test_target.py::test_target"})(),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.current_task_id == target.id


def test_test_generation_uses_multiple_rounds_for_inspect_write_and_final_json(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        seen.append(kwargs)
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert seen[0]["max_rounds"] == runner_mod.TEST_WRITER_MAX_ROUNDS
    assert runner_mod.TEST_WRITER_MAX_ROUNDS > 1
    assert "Test-design protocol" in seen[0]["prompt"]


def test_test_generation_continues_after_a_round_slice(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    _task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            (tests_dir / "test_new.py").write_text("def test_new(): assert False\n", encoding="utf-8")
            kwargs["stats"].stop_reason = "max_rounds"
            return "[goal_test_writer] still using tools"
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert len(calls) == 2
    assert "Continue the same test-writing conversation" in calls[1]["prompt"]
    assert calls[0]["conversation"] is calls[1]["conversation"]
    assert calls[0]["deadline"] == calls[1]["deadline"]


def test_test_generation_continues_after_max_tokens_with_retained_context(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    _task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["stats"].stop_reason = "max_tokens"
            return "partial test design"
        (tests_dir / "test_new.py").write_text("def test_new(): assert False\n", encoding="utf-8")
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert len(calls) == 2
    assert calls[0]["conversation"] is calls[1]["conversation"]
    assert "source-inspection and test-design slice is complete" in calls[1]["prompt"]
    assert "next tool call must create" in calls[1]["prompt"]
    assert state.phase == GoalPhase.SELECT_TASK.value


def test_test_generation_rejects_final_json_until_a_new_test_artifact_exists(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '{"test_selectors": [], "case_selectors": {"AC1": []}}'
        (tests_dir / "test_new.py").write_text("def test_new(): assert False\n", encoding="utf-8")
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert len(calls) == 2
    assert calls[0]["conversation"] is calls[1]["conversation"]
    assert "source-inspection and test-design slice is complete" in calls[1]["prompt"]
    assert "next tool call must create" in calls[1]["prompt"]
    assert load_task(task.id).verification_spec["source"] == "generated"


def test_test_generation_round_exhaustion_without_artifact_is_stalled_not_provider_unavailable(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    _task, state = _needs_generation_task(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def stalled_writer(**kwargs):
        calls.append(kwargs)
        kwargs["stats"].stop_reason = "max_rounds"
        return "[goal_test_writer] failed: empty response (17 tools, 94.2s)"

    monkeypatch.setattr(runner_mod, "run_agent_task", stalled_writer)
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: TestCatalog())

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert len(calls) == runner_mod.TEST_WRITER_MAX_IDLE_CHUNKS
    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "test_writer_stalled"
    assert "consecutive round slices" in state.last_error
    assert "provider" not in state.last_error.lower()


def test_test_generation_stall_preserves_written_test_for_json_completion(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            (tests_dir / "test_new.py").write_text("def test_new(): assert False\n", encoding="utf-8")
        if len(calls) <= 4:
            kwargs["stats"].stop_reason = "max_rounds"
            return "[goal_test_writer] still using tools"
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert len(calls) == 5
    assert calls[-1]["tools_override"] == ()
    assert (tests_dir / "test_new.py").exists()
    assert load_task(task.id).verification_spec["source"] == "generated"


def test_test_generation_recovers_written_test_when_writer_omits_final_json(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.tasks import load_task

    task, state = _needs_generation_task(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    calls = []
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def writer(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            (tests_dir / "test_new.py").write_text("def test_new(): assert False\n", encoding="utf-8")
            kwargs["stats"].stop_reason = "empty_response"
            return "[goal_test_writer] failed: empty response (2 tools, 1.0s)"
        return '{"test_selectors":["tests/test_new.py::test_new"],"case_selectors":{"AC1":["tests/test_new.py::test_new"]}}'

    monkeypatch.setattr(runner_mod, "run_agent_task", writer)
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

    assert len(calls) == 2
    assert calls[1]["tools_override"] == ()
    assert load_task(task.id).verification_spec["source"] == "generated"
    assert (tests_dir / "test_new.py").exists()


def test_test_generation_empty_response_without_a_test_is_not_provider_unavailable(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    _task, state = _needs_generation_task(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)

    def empty_writer(**kwargs):
        kwargs["stats"].stop_reason = "empty_response"
        return "[goal_test_writer] failed: empty response (2 tools, 1.0s)"

    monkeypatch.setattr(runner_mod, "run_agent_task", empty_writer)
    monkeypatch.setattr(runner_mod, "collect_pytest_catalog", lambda workspace: TestCatalog())

    GoalRunner(state=state, history=[], context={}, binding=None)._prepare_tests(state)

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "test_writer_empty_response"
    assert "did not submit a final result" in state.last_error


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


def test_node_goal_regression_creates_a_node_bound_repair_task(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import GoalRegressionDecision
    from harness.verification.node_adapter import NodeTestAdapter

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    state = GoalState.new(target="x", verification="npm test", workspace=str(tmp_path))
    state.phase = GoalPhase.FULL_VERIFY.value
    state.final_verification = {
        "stdout_tail": "not ok 1 - queue drains in FIFO order",
        "exit_code": 1,
    }
    monkeypatch.setattr(runner_mod, "save_goal", lambda current: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args: None)
    monkeypatch.setattr(
        repair_mod,
        "plan_goal_regression_repair",
        lambda *args, **kwargs: GoalRegressionDecision(
            "create_repair_task", None, "restore queue ordering", "new Node repair"
        ),
    )
    catalog = NodeTestCatalog(
        ("test/queue.test.ts::queue drains in FIFO order",),
        ("test/queue.test.ts",),
    )
    monkeypatch.setattr(NodeTestAdapter, "discover", lambda self, context: catalog)

    GoalRunner(state=state, history=[], context={}, binding=None)._queue_goal_repair(
        state, "full verification failed with exit code 1"
    )

    repair = tasks.load_task(state.current_task_id)
    assert repair.verification_spec["adapter"] == "node"
    assert repair.verification_spec["command"] == "node --import tsx --test test/queue.test.ts"
    assert repair.verification_spec["selectors"] == ["test/queue.test.ts::queue drains in FIFO order"]


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


def test_repair_plan_json_format_failure_resumes_current_task(tmp_path, monkeypatch):
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

    assert state.phase == GoalPhase.ACT.value
    assert state.stop_reason in {None, ""}
    repaired = tasks.load_task(task.id)
    assert repaired.repair_history[-1]["event"] == "repair_planner_format_fallback"
    assert repaired.repair_history[-1]["format_fallback"] is True


def test_repair_plan_json_format_failure_replans_after_repair_threshold(tmp_path, monkeypatch):
    import harness.goal.repair as repair_mod
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.repair import RepairDecision

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("repair", "behavior", verification_spec={})
    tasks.claim_task(task.id)
    tasks.record_task_evaluation(task.id, {"passed": False, "route": "implementation_fix"})
    for _ in range(4):
        tasks.record_task_repair(task.id, {"repair_epoch": 0, "action": "implementation_fix"})
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
    captured = {}
    monkeypatch.setattr(
        GoalRunner,
        "_replan_remaining_tasks",
        lambda self, state, *, task, evaluation, reason: captured.update(
            task=task.id, route=evaluation["route"], reason=reason
        ),
    )

    GoalRunner(state=state, history=[], context={}, binding=None)._repair_plan(state)

    repaired = tasks.load_task(task.id)
    assert captured["task"] == task.id
    assert captured["route"] == "replan"
    assert repaired.repair_history[-1]["action"] == "replan"
    assert repaired.repair_history[-1]["format_fallback"] is True


def test_malformed_repair_output_preserves_route_and_records_bounded_audit():
    from harness.goal.repair import plan_task_repair

    state = GoalState.new(target="repair", verification="pytest -q", workspace=".")
    task = SimpleNamespace(
        id="task_demo",
        subject="repair",
        description="repair behavior",
        primary_write=[],
        planned_new=[],
        conditional_write=[],
        read_envelope=[],
        acceptance_cases=[],
        verification_spec={},
        last_error="",
        start_snapshot="",
        start_diff="",
    )
    replies = iter((
        "first malformed C:/Users/example/workspace token=private-value",
        "second malformed /home/example/project password: private-value",
    ))

    decision = plan_task_repair(
        state,
        task,
        {"passed": False, "route": "replan", "summary": "task boundaries conflict"},
        cwd=".",
        runner=lambda **kwargs: next(replies),
    )

    assert decision.action == "replan"
    assert decision.format_fallback is True
    assert "C:/Users" not in decision.raw_output_tail
    assert "/home/example" not in decision.correction_output_tail
    assert "private-value" not in decision.raw_output_tail
    assert "private-value" not in decision.correction_output_tail
    assert "[redacted-host-path]" in decision.raw_output_tail
    assert "token=[redacted]" in decision.raw_output_tail
    assert len(decision.raw_output_sha256) == 64
    assert len(decision.correction_output_sha256) == 64


def test_repair_planner_runs_tool_free():
    from harness.goal.repair import plan_task_repair

    state = GoalState.new(target="repair", verification="pytest -q", workspace=".")
    task = SimpleNamespace(
        id="task_demo", subject="repair", description="repair behavior",
        primary_write=[], planned_new=[], conditional_write=[], read_envelope=[],
        acceptance_cases=[], verification_spec={}, last_error="", start_snapshot="", start_diff="",
    )
    calls = []

    decision = plan_task_repair(
        state,
        task,
        {"passed": False, "route": "implementation_fix"},
        cwd=".",
        runner=lambda **kwargs: calls.append(kwargs) or '{"action":"implementation_fix","instructions":"apply fix"}',
    )

    assert decision.action == "implementation_fix"
    assert calls[0]["tools_override"] == ()


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


def test_no_progress_still_routes_through_verification(tmp_path, monkeypatch):
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

    assert state.phase == GoalPhase.VERIFY.value
    assert state.status == "running"
    assert state.last_error is None


def test_round_limit_with_repeated_no_progress_still_routes_through_verification(tmp_path, monkeypatch):
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

    def capped_worker(*args, stats, **kwargs):
        stats.stop_reason = "max_rounds"
        return "still exploring"

    monkeypatch.setattr(runner_mod, "run_agent_task", capped_worker)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(runner, "_progress_snapshot", lambda state: ("unchanged",))

    runner._act(state)

    assert state.phase == GoalPhase.ROLLOVER.value
    assert state.worker_rollovers == 1


def test_repeated_no_progress_routes_to_repair_only_after_failed_verification(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = tasks.create_task("stuck task", "keep working", verification_spec={})
    tasks.claim_task(task.id)
    state = GoalState.new(target="stuck task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.phase = GoalPhase.VERIFY.value
    state.no_progress_count = 2
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args, **kwargs: None)
    failed = tasks.load_task(task.id)
    failed.verification_state = "failing"
    failed.last_error = "bound test failed"
    monkeypatch.setattr(runner_mod, "verify_task_command", lambda *args, **kwargs: failed)

    runner_mod.GoalRunner(state=state, history=[], context={}, binding=None)._verify(state)

    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert "verification still fails" in state.last_error


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


def test_unavailable_permission_supervisor_pauses_as_provider_failure(tmp_path, monkeypatch):
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
    assert state.stop_reason == "provider_unavailable"
    assert "supervisor is unavailable" in state.last_error


def _run_supervised_permission_boundary(
    tmp_path,
    monkeypatch,
    *,
    decision,
    requested_path="src/shared.py",
    envelope=("src/current.py", "src/shared.py"),
    tool="edit_file",
    bind_requested_source_test=False,
):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.coordinator import SupervisorRun

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    verification_spec = {}
    if bind_requested_source_test:
        source_path = tmp_path / requested_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("export const source = true;\n", encoding="utf-8")
        test_path = tmp_path / "test" / "permission-boundary.test.ts"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            f"import {{source}} from '../{requested_path}';\nvoid source;\n",
            encoding="utf-8",
        )
        verification_spec = {
            "test_files": ["test/permission-boundary.test.ts"],
            "test_hashes": {
                "test/permission-boundary.test.ts": hashlib.sha256(test_path.read_bytes()).hexdigest(),
            },
        }
    task = tasks.create_task(
        "permission task",
        "keep working",
        verification_spec=verification_spec,
        scope_paths=["src/current.py"],
    )
    tasks.claim_task(task.id)
    state = GoalState.new(target="permission task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.task_plan = [{"name": "permission task", "scope_paths": list(envelope)}]
    state.phase = GoalPhase.ACT.value
    monkeypatch.setattr(runner_mod, "save_goal", lambda state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args, **kwargs: None)

    def worker(*args, stats, **kwargs):
        runner_mod.mark_goal_permission_pending({
            "tool": tool,
            "path": requested_path,
            "resource": requested_path,
            "reason": "outside current Task scope",
            "source": "goal_write_scope",
        })
        stats.tool_names.append(tool)
        stats.write_paths.append(requested_path)
        return "worker reached a capability boundary"

    monkeypatch.setattr(runner_mod, "run_agent_task", worker)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(runner, "_progress_snapshot", lambda _state: ("unchanged",))
    monkeypatch.setattr(runner, "_validate_task_scope", lambda _state, _task: None)
    monkeypatch.setattr(
        runner,
        "_review_supervisor_boundary",
        lambda *args, **kwargs: SupervisorRun("obs-1", len(state.transition_log), decision),
    )

    runner._act(state)
    return state, tasks.load_task(task.id)


def test_supervisor_can_expand_exact_task_scope_inside_goal_envelope(tmp_path, monkeypatch):
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        decision=SupervisorDecision(
            "expand_scope",
            "Shared module is part of the approved Goal.",
            next_step="Edit the shared module and rerun the Task.",
            scope_paths=("src/shared.py",),
        ),
    )

    assert state.phase == GoalPhase.ACT.value
    assert state.status == "running"
    assert task.scope_paths == ["src/current.py", "src/shared.py"]
    assert state.permission_boundary_attempts[task.id] == 1


def test_supervisor_can_amend_scope_for_an_unchanged_bound_test_import(tmp_path, monkeypatch):
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        envelope=("src/current.py",),
        bind_requested_source_test=True,
        decision=SupervisorDecision(
            "amend_scope",
            "The frozen Task test directly imports the omitted source module.",
            next_step="Edit the requested source module and rerun the Task.",
            scope_paths=("src/shared.py",),
            confidence="high",
        ),
    )

    assert state.phase == GoalPhase.ACT.value
    assert state.status == "running"
    assert task.scope_paths == ["src/current.py", "src/shared.py"]
    assert state.task_plan[0]["scope_paths"] == ["src/current.py", "src/shared.py"]


def test_verification_failure_reconciles_a_test_proven_scope_omission(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.coordinator import SupervisorDecision, SupervisorRun

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    source_path = tmp_path / "src" / "shared.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("export const shared = true;\n", encoding="utf-8")
    test_path = tmp_path / "test" / "scope.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("import {shared} from '../src/shared.py';\nvoid shared;\n", encoding="utf-8")
    task = tasks.create_task(
        "permission task",
        "keep working",
        scope_paths=["src/current.py"],
        verification_spec={
            "test_files": ["test/scope.test.ts"],
            "test_hashes": {"test/scope.test.ts": hashlib.sha256(test_path.read_bytes()).hexdigest()},
        },
    )
    tasks.claim_task(task.id)
    task = tasks.load_task(task.id)
    task.evidence = [{"exit_code": 1, "stdout_tail": "shared behavior failed"}]
    tasks.save_task(task)
    state = GoalState.new(target="permission task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.task_plan = [{"name": task.subject, "scope_paths": ["src/current.py"]}]
    state.phase = GoalPhase.VERIFY.value
    state.supervision = {"observation_revision": 1}
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args, **kwargs: None)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(
        runner,
        "_review_supervisor_boundary",
        lambda *args, **kwargs: SupervisorRun(
            "obs-1", 1,
            SupervisorDecision(
                "amend_scope",
                "The frozen test imports the omitted source module.",
                scope_paths=("src/shared.py",),
                confidence="high",
            ),
        ),
    )

    assert runner._try_verification_scope_amendment(state, task)

    updated = tasks.load_task(task.id)
    assert state.phase == GoalPhase.ACT.value
    assert updated.scope_paths == ["src/current.py", "src/shared.py"]
    assert state.task_plan[0]["scope_paths"] == ["src/current.py", "src/shared.py"]


def test_verification_scope_reconciliation_does_not_depend_on_supervisor_availability(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    import harness.tasks as tasks
    from harness.goal.coordinator import SupervisorDecision, SupervisorRun

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    source_path = tmp_path / "src" / "shared.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("export const shared = true;\n", encoding="utf-8")
    test_path = tmp_path / "test" / "scope.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("import {shared} from '../src/shared.py';\nvoid shared;\n", encoding="utf-8")
    task = tasks.create_task(
        "permission task",
        "keep working",
        scope_paths=["src/current.py"],
        verification_spec={
            "test_files": ["test/scope.test.ts"],
            "test_hashes": {"test/scope.test.ts": hashlib.sha256(test_path.read_bytes()).hexdigest()},
        },
    )
    tasks.claim_task(task.id)
    task = tasks.load_task(task.id)
    task.evidence = [{"exit_code": 1, "stdout_tail": "shared behavior failed"}]
    tasks.save_task(task)
    state = GoalState.new(target="permission task", verification="pytest -q", workspace=str(tmp_path))
    state.current_task_id = task.id
    state.task_ids = [task.id]
    state.task_plan = [{"name": task.subject, "scope_paths": ["src/current.py"]}]
    state.phase = GoalPhase.VERIFY.value
    state.supervision = {"observation_revision": 1}
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_mod, "_emit_goal", lambda *args, **kwargs: None)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    monkeypatch.setattr(
        runner,
        "_review_supervisor_boundary",
        lambda *args, **kwargs: SupervisorRun(
            "obs-1", 1,
            SupervisorDecision(
                "watch",
                "Supervisor is unavailable.",
                unavailable=True,
                error="provider_error",
            ),
        ),
    )

    assert runner._try_verification_scope_amendment(state, task)

    updated = tasks.load_task(task.id)
    assert state.phase == GoalPhase.ACT.value
    assert updated.scope_paths == ["src/current.py", "src/shared.py"]
    assert "deterministic test evidence" in updated.last_error


def test_frozen_test_imports_include_multiline_named_imports(tmp_path, monkeypatch):
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    source_path = tmp_path / "src" / "shared.ts"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("export const shared = true;\n", encoding="utf-8")
    test_path = tmp_path / "test" / "scope.test.ts"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import {\n  shared,\n} from '../src/shared.js';\nvoid shared;\n",
        encoding="utf-8",
    )
    task = tasks.create_task(
        "import scan",
        "keep working",
        verification_spec={
            "test_files": ["test/scope.test.ts"],
            "test_hashes": {"test/scope.test.ts": hashlib.sha256(test_path.read_bytes()).hexdigest()},
        },
    )
    state = GoalState.new(target="import scan", verification="pytest -q", workspace=str(tmp_path))
    runner = GoalRunner(state=state, history=[], context={}, binding=None)

    assert runner._frozen_test_imports(state, task) == {"src/shared.ts"}


def test_frozen_test_imports_include_workspace_python_module_imports(tmp_path, monkeypatch):
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    source_path = tmp_path / "harness" / "goal" / "runner.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("class GoalRunner: pass\n", encoding="utf-8")
    test_path = tmp_path / "tests" / "test_scope.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "from harness.goal.runner import GoalRunner\n\nassert GoalRunner\n",
        encoding="utf-8",
    )
    task = tasks.create_task(
        "import scan",
        "keep working",
        verification_spec={
            "test_files": ["tests/test_scope.py"],
            "test_hashes": {"tests/test_scope.py": hashlib.sha256(test_path.read_bytes()).hexdigest()},
        },
    )
    state = GoalState.new(target="import scan", verification="pytest -q", workspace=str(tmp_path))
    runner = GoalRunner(state=state, history=[], context={}, binding=None)

    assert runner._frozen_test_imports(state, task) == {"harness/goal/runner.py"}


def test_routine_goal_events_do_not_start_parallel_supervisor_requests(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="supervision", verification="pytest -q", workspace=str(tmp_path))
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    observed = []
    runner._supervisor = type("Supervisor", (), {"observe": lambda _self, observation: observed.append(observation) or "obs-1"})()
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)

    runner._observe_supervisor("agent_finished", detail={"agent_type": "goal_worker"})

    assert observed == []


def test_supervisor_cannot_expand_scope_outside_goal_envelope(tmp_path, monkeypatch):
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        requested_path="docs/outside.py",
        envelope=("src/current.py", "src/shared.py"),
        decision=SupervisorDecision(
            "expand_scope",
            "This path might help.",
            scope_paths=("docs/outside.py",),
        ),
    )

    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == "permission_wait"
    assert task.scope_paths == ["src/current.py"]
    assert "outside the approved Goal scope envelope" in state.last_error


def test_supervisor_redirect_retries_act_with_durable_guidance(tmp_path, monkeypatch):
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        decision=SupervisorDecision(
            "redirect",
            "The worker chose the wrong file.",
            next_step="Use the existing adapter in src/current.py.",
        ),
    )

    assert state.phase == GoalPhase.ACT.value
    assert state.status == "running"
    assert "Use the existing adapter" in task.last_error


def test_supervisor_continue_clears_stale_permission_request_and_verifies(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        decision=SupervisorDecision(
            "continue",
            "The worker completed the scoped implementation without the requested file.",
            next_step="Verify the in-scope implementation.",
        ),
    )

    assert state.phase == GoalPhase.VERIFY.value
    assert state.status == "running"
    assert task.scope_paths == ["src/current.py"]
    assert task.id not in state.permission_boundary_attempts
    assert runner_mod.goal_permission_pending() is False


def test_supervisor_permission_replan_routes_to_repair_plan(tmp_path, monkeypatch):
    from harness.goal.coordinator import SupervisorDecision

    state, task = _run_supervised_permission_boundary(
        tmp_path,
        monkeypatch,
        decision=SupervisorDecision(
            "replan",
            "Task ownership is wrong.",
            next_step="Rebuild the Task boundary inside the frozen Goal contract.",
        ),
    )

    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert state.status == "running"
    assert "Rebuild the Task boundary" in task.last_error


def test_terminal_supervisor_replan_is_consumed_as_a_safe_resume_directive(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.goal.coordinator import SupervisorDecision, SupervisorRun

    state = GoalState.new(target="recover", verification="pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = "paused"
    state.resume_phase = GoalPhase.ACT.value
    state.current_task_id = "task-current"
    state.transition_log = [{"from": "act", "to": "paused", "reason": "worker_stalled"}]
    state.supervision = {"observation_revision": 4, "history": []}
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_mod, "_emit", lambda *args, **kwargs: None)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)

    runner._record_supervisor_run(
        SupervisorRun(
            "obs-4", 4,
            SupervisorDecision("replan", "The task needs a narrower repair plan.", next_step="Reassess the failing seam."),
        ),
        event="terminal_failure",
    )

    assert runner_mod._supervisor_recovery_target(state) == GoalPhase.REPAIR_PLAN.value
    assert state.supervision["recovery"]["task_id"] == "task-current"
    runner_mod._consume_supervisor_recovery(state)
    assert state.supervision["recovery"]["consumed_at"]
    assert runner_mod._supervisor_recovery_target(state) is None


def test_stale_terminal_supervisor_recovery_cannot_override_newer_checkpoint(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod

    state = GoalState.new(target="recover", verification="pytest -q", workspace=str(tmp_path))
    state.resume_phase = GoalPhase.ACT.value
    state.transition_log = [{"from": "act", "to": "paused"}, {"from": "paused", "to": "act"}]
    state.supervision = {
        "recovery": {
            "action": "redirect",
            "target_phase": GoalPhase.REPAIR_PLAN.value,
            "transition_revision": 1,
        },
    }

    assert runner_mod._supervisor_recovery_target(state) is None


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
                "scope_paths": [],
                "evidence_refs": [],
                "test_strategy": "",
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


def test_goal_event_snapshot_exposes_global_supervision(tmp_path, monkeypatch):
    from harness.goal.runner import goal_event_payload

    _task, state = _needs_generation_task(tmp_path, monkeypatch)
    state.supervision = {
        "status": "attention",
        "model": "deepseek-v4-pro",
        "latest": {"action": "redirect", "summary": "Change direction."},
    }

    payload = goal_event_payload(state)

    assert payload["supervision"]["status"] == "attention"
    assert payload["supervision"]["latest"]["action"] == "redirect"


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
    monkeypatch.setattr(
        runner_mod,
        "_emit_goal",
        lambda event_type, current, **metadata: emitted.append((event_type, current.status, metadata)),
    )

    runner_mod.emit_current_goal_status(include_terminal=False)
    assert emitted == [("goal_status", "running", {})]

    emitted.clear()
    runner_mod.emit_current_goal_status(include_terminal=False, hydrated=True)
    assert emitted == [("goal_status", "running", {"hydrated": True})]

    state.status = "done"
    runner_mod.emit_current_goal_status(include_terminal=False)
    assert emitted == [("goal_status", "running", {"hydrated": True})]

    runner_mod.emit_current_goal_status(include_terminal=True)
    assert emitted[-1] == ("goal_status", "done", {})


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
