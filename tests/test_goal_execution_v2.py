"""Execution-v2 state machine and terminal-task regression tests."""

from harness.goal.engine import GoalEngine
from harness.goal.models import GoalPhase, GoalState
from harness.goal.planner import GoalPlan, GoalPlanningError, TaskPlan, VerificationSpec
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


def test_repair_plan_can_recheck_verification_without_another_worker_turn():
    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    state.phase = GoalPhase.REPAIR_PLAN.value

    GoalEngine().transition(state, GoalPhase.VERIFY, "verification_contract_recheck")

    assert state.phase == GoalPhase.VERIFY.value


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
    downstream = create_task("wire downstream behavior", "complete the dependent UI path", blockedBy=[active.id])
    downstream.acceptance_cases = [{"id": "AC2", "given": "the implementation exists", "when": "the UI runs", "then": "the dependent path works"}]
    downstream.primary_write = ["src/downstream.py"]
    downstream.read_envelope = ["src/example.py"]
    save_task(downstream)
    grandchild = create_task(
        "finish final integration",
        "complete the later integration path",
        blockedBy=[downstream.id],
    )
    save_task(grandchild)

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.current_task_id = active.id
    state.task_ids = [completed.id, active.id, downstream.id, grandchild.id]
    state.task_name_ids = {
        completed.subject: completed.id,
        active.subject: active.id,
        downstream.subject: downstream.id,
        grandchild.subject: grandchild.id,
    }
    state.goal_contract = {"summary": "x"}
    generated = tmp_path / "src" / "generated_during_execution.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("# generated\n", encoding="utf-8")

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
    planner_calls = []

    def plan_remaining(*args, **kwargs):
        planner_calls.append(kwargs)
        return replacement_plan

    monkeypatch.setattr(runner_module, "plan_tasks", plan_remaining)
    state.execution_trace = [{
        "event": "implementation_slice",
        "task_id": active.id,
        "detail": {"write_paths": ["src/generated_during_execution.py"]},
    }]

    runner = runner_module.GoalRunner(state=state, history=[], context={}, binding=None)
    runner._replan_remaining_tasks(
        state,
        task=active,
        evaluation={
            "route": "replan",
            "replan_trigger": "execution_preflight",
            "findings": [{"issue": "task boundary was wrong", "evidence": "execution preflight"}],
        },
        reason="task boundary was wrong",
    )

    replacement_id = state.task_name_ids["replacement implementation"]
    replacement = load_task(replacement_id)
    assert state.phase == GoalPhase.SELECT_TASK.value
    assert state.task_ids == [completed.id, replacement_id, downstream.id, grandchild.id]
    assert load_task(active.id).status == "cancelled"
    assert load_task(downstream.id).status != "cancelled"
    assert load_task(grandchild.id).status != "cancelled"
    assert load_task(downstream.id).blockedBy == [replacement_id]
    assert load_task(grandchild.id).blockedBy == [downstream.id]
    assert replacement.blockedBy == [completed.id]
    assert "src/generated_during_execution.py" in planner_calls[0]["execution_workspace_paths"]
    assert planner_calls[0]["replacement_scope"] == ({
        "task_id": active.id,
        "name": active.subject,
        "behavior": active.description,
        "acceptance_cases": [],
        "primary_write": [],
        "planned_new": [],
        "conditional_write": [],
        "read_envelope": [],
        "verification": {},
    },)


def test_execution_replan_resumes_saved_candidate_with_reviewer_findings(tmp_path, monkeypatch):
    import harness.goal.discovery_store as discovery_store
    import harness.goal.runner as runner_module
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    monkeypatch.setattr(discovery_store, "load_manifest", lambda *_: {"repo_files": [], "evidence": []})
    monkeypatch.setattr(runner_module, "save_goal", lambda *_: None)

    active = create_task("old implementation", "outdated task")
    active.status = "in_progress"
    save_task(active)
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.current_task_id = active.id
    state.task_ids = [active.id]
    state.task_name_ids = {active.subject: active.id}
    state.goal_contract = {"summary": "x"}
    candidate = GoalPlan(
        contract={"summary": "x"},
        tasks=(TaskPlan(
            name="replacement implementation",
            behavior="finish the unfinished behavior",
            acceptance_cases=(),
            verification_spec=VerificationSpec(),
            primary_write=("src/example.py",),
        ),),
    )
    rejection = {
        "approved": False,
        "summary": "scope needs correction",
        "findings": [{"severity": "high", "task": "replacement implementation", "issue": "scope", "repair": "narrow it"}],
    }
    calls = []

    def first_plan(*_args, **kwargs):
        calls.append(kwargs)
        kwargs["candidate_callback"](candidate)
        kwargs["review_callback"](candidate, rejection)
        raise GoalPlanningError("response contains malformed JSON")

    monkeypatch.setattr(runner_module, "plan_tasks", first_plan)
    runner = runner_module.GoalRunner(state=state, history=[], context={}, binding=None)
    evaluation = {
        "route": "replan",
        "replan_trigger": "execution_preflight",
        "findings": [{"issue": "scope", "evidence": "preflight"}],
    }
    runner._replan_remaining_tasks(state, task=active, evaluation=evaluation, reason="scope changed")

    assert state.execution_replan_checkpoint["stage"] == "repair"
    assert state.execution_replan_checkpoint["version"] == runner_module.EXECUTION_REPLAN_CHECKPOINT_VERSION
    assert state.execution_replan_checkpoint["review"] == rejection

    state.phase = GoalPhase.REPAIR_PLAN.value
    state.status = "running"

    def resumed_plan(*_args, **kwargs):
        calls.append(kwargs)
        assert kwargs["candidate_plan"] is not None
        assert kwargs["review_feedback"] == rejection
        return candidate

    monkeypatch.setattr(runner_module, "plan_tasks", resumed_plan)
    runner._replan_remaining_tasks(state, task=active, evaluation=evaluation, reason="scope changed")

    assert len(calls) == 2
    assert state.execution_replan_checkpoint == {}


def test_execution_replan_discards_a_versionless_checkpoint(tmp_path, monkeypatch):
    import harness.goal.runner as runner_module
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    active = create_task("old implementation", "outdated task")
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.goal_contract = {"summary": "x"}
    state.execution_replan_checkpoint = {
        "goal_id": state.id,
        "task_id": active.id,
        "key": "obsolete",
        "stage": "repair",
        "candidate": {"tasks": []},
        "review": {},
    }

    plan, review = runner_module.GoalRunner(state=state, history=[], context={}, binding=None)._load_execution_replan_checkpoint(
        state, active, key="obsolete",
    )

    assert plan is None
    assert review is None
    assert state.execution_replan_checkpoint == {}


def test_execution_replan_stops_repeating_the_same_rejected_checkpoint(tmp_path, monkeypatch):
    import harness.goal.discovery_store as discovery_store
    import harness.goal.runner as runner_module
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    monkeypatch.setattr(discovery_store, "load_manifest", lambda *_: {"repo_files": [], "evidence": []})
    monkeypatch.setattr(runner_module, "save_goal", lambda *_: None)

    active = create_task("old implementation", "outdated task")
    active.status = "in_progress"
    save_task(active)
    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.current_task_id = active.id
    state.task_ids = [active.id]
    state.task_name_ids = {active.subject: active.id}
    state.goal_contract = {"summary": "x"}

    calls = []

    def rejected_plan(*_args, **_kwargs):
        calls.append(True)
        raise GoalPlanningError("review rejected replacement")

    monkeypatch.setattr(runner_module, "plan_tasks", rejected_plan)
    evaluation = {
        "route": "replan",
        "replan_trigger": "execution_preflight",
        "findings": [{"issue": "scope", "evidence": "preflight"}],
    }
    runner = runner_module.GoalRunner(state=state, history=[], context={}, binding=None)
    runner._replan_remaining_tasks(state, task=active, evaluation=evaluation, reason="scope changed")
    assert calls == [True]
    assert state.execution_replan_checkpoint["failure_count"] == 1
    first_key = state.execution_replan_checkpoint["key"]
    # A changing worktree inventory must not make the same rejected replan
    # look like a fresh checkpoint and bypass the retry guard.
    (tmp_path / "generated-after-failure.txt").write_text("workspace changed\n", encoding="utf-8")

    state.phase = GoalPhase.REPAIR_PLAN.value
    state.status = "running"
    runner._replan_remaining_tasks(state, task=active, evaluation=evaluation, reason="scope changed")
    assert calls == [True, True]
    assert state.execution_replan_checkpoint["failure_count"] == 2
    assert state.execution_replan_checkpoint["key"] == first_key

    state.phase = GoalPhase.REPAIR_PLAN.value
    state.status = "running"
    runner._replan_remaining_tasks(state, task=active, evaluation=evaluation, reason="scope changed")
    assert calls == [True, True]
    assert state.stop_reason == "task_blocked"
    assert state.execution_trace[-1]["event"] == "execution_replan_exhausted"


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
