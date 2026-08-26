"""Execution-v2 state machine and terminal-task regression tests."""

from harness.goal.engine import GoalEngine
from harness.goal.models import GoalPhase, GoalState
from harness.goal.planner import GoalPlan, TaskPlan, VerificationSpec
from harness.tasks import block_task, create_task, load_task, save_task


def test_execution_preflight_is_required_between_selection_and_claim():
    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    engine = GoalEngine()

    engine.transition(state, GoalPhase.SELECT_TASK, "initialized")
    engine.transition(state, GoalPhase.PREPARE_EXECUTION, "task_selected")
    engine.transition(state, GoalPhase.CLAIM, "execution_preflight_passed")

    assert state.phase == GoalPhase.CLAIM.value


def test_progress_rollover_runs_a_machine_checkpoint_before_another_extension():
    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.phase = GoalPhase.ACT.value
    engine = GoalEngine()

    engine.transition(state, GoalPhase.ROLLOVER, "worker_progress_extended")
    engine.transition(state, GoalPhase.VERIFY, "worker_progress_checkpoint")

    assert state.phase == GoalPhase.VERIFY.value


def test_clean_check_can_route_to_structured_repair_analysis():
    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.phase = GoalPhase.CLEAN_CHECK.value

    GoalEngine().transition(state, GoalPhase.REPAIR_PLAN, "clean_check_failed")

    assert state.phase == GoalPhase.REPAIR_PLAN.value


def test_blocked_task_is_explicit_and_preserves_failure_reason(tmp_path, monkeypatch):
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    task = create_task("external runner", "requires a unavailable runtime")

    blocked = block_task(task.id, error="Docker daemon is unavailable")
    loaded = load_task(task.id)

    assert blocked.status == "blocked"
    assert loaded.status == "blocked"
    assert loaded.last_error == "Docker daemon is unavailable"


def test_execution_replan_replaces_only_unfinished_tasks(tmp_path, monkeypatch):
    import harness.goal.discovery_store as discovery_store
    import harness.goal.runner as runner_module
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    monkeypatch.setattr(discovery_store, "load_manifest", lambda *_: {"repo_files": [], "evidence": []})
    monkeypatch.setattr(runner_module, "save_goal", lambda *_: None)

    completed = create_task("prepare storage", "completed dependency")
    completed.status = "completed"
    save_task(completed)
    active = create_task("old implementation", "outdated task")
    active.status = "in_progress"
    save_task(active)

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.current_task_id = active.id
    state.task_ids = [completed.id, active.id]
    state.task_name_ids = {completed.subject: completed.id, active.subject: active.id}
    state.goal_contract = {"summary": "x"}

    replacement_plan = GoalPlan(
        contract={"summary": "x"},
        tasks=(TaskPlan(
            name="replacement implementation",
            behavior="finish the unfinished behavior",
            depends_on=(completed.subject,),
            acceptance_cases=(),
            verification_spec=VerificationSpec(),
            primary_write=("src/example.py",),
        ),),
        review={"approved": True},
    )
    monkeypatch.setattr(runner_module, "plan_tasks", lambda *_, **__: replacement_plan)

    runner = runner_module.GoalRunner(state=state, history=[], context={}, binding=None)
    runner._replan_remaining_tasks(
        state,
        task=active,
        evaluation={"route": "replan"},
        reason="task boundary was wrong",
    )

    replacement_id = state.task_name_ids["replacement implementation"]
    replacement = load_task(replacement_id)
    assert state.phase == GoalPhase.SELECT_TASK.value
    assert state.task_ids == [completed.id, replacement_id]
    assert load_task(active.id).status == "cancelled"
    assert replacement.blockedBy == [completed.id]


def test_execution_replan_manifest_uses_current_worktree_files(tmp_path):
    from harness.goal.runner import _execution_replan_manifest

    sandbox = tmp_path / "harness" / "goal" / "sandbox.py"
    sandbox.parent.mkdir(parents=True)
    sandbox.write_text("class Sandbox: pass\n", encoding="utf-8")
    manifest, paths = _execution_replan_manifest(
        {
            "repo_files": ["docs/goal-shell-sandbox-requirements.md"],
            "evidence": [{"id": "E1", "path": "docs/goal-shell-sandbox-requirements.md", "claim": "old input"}],
        },
        tmp_path,
        scope_paths=("harness/goal/sandbox.py", "docs/goal-shell-sandbox-requirements.md"),
    )

    assert manifest["repo_files"] == ["harness/goal/sandbox.py"]
    assert paths == ("harness/goal/sandbox.py",)
    assert any(item["path"] == "harness/goal/sandbox.py" for item in manifest["evidence"])


def test_missing_case_selectors_requires_nonempty_machine_bindings():
    from harness.goal.runner import GoalRunner

    missing = GoalRunner._missing_case_selectors(
        {"AC1", "AC2", "AC3"},
        {"AC1": ["tests/test_example.py::test_one"], "AC2": []},
    )

    assert missing == ["AC2", "AC3"]
