"""Supervisor guidance only resumes a Goal through durable, bounded gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.goal.coordinator import SupervisorDecision, SupervisorRun
from harness.goal.models import GoalPhase, GoalState, StopReason
from harness.goal.runner import GoalRunner
from harness.tasks import create_task, load_task, save_task


def _paused_task(tmp_path, monkeypatch):
    import harness.goal.runner as runner_module
    import harness.tasks as task_module

    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path / ".tasks")
    monkeypatch.setattr(runner_module, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_module, "_emit_goal", lambda *_args, **_kwargs: None)
    task = create_task("repair ownership", "fix the failed behavior")
    task.status = "in_progress"
    task.verification_state = "failing"
    task.last_error = "bound test failed in src/app.py"
    save_task(task)
    state = GoalState.new(target="fix app", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    state.task_plan = [{"name": task.subject}]
    state.task_name_ids = {task.subject: task.id}
    state.task_ids = [task.id]
    state.current_task_id = task.id
    state.initialization_complete = True
    state.execution_approved = True
    state.phase = GoalPhase.REPAIR_PLAN.value
    state.last_error = task.last_error
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    runner.engine.transition(
        state, GoalPhase.PAUSED, "repair_planner_failed",
        stop_reason=StopReason.task_blocked.value,
    )
    state.supervision = {"observation_revision": 1}
    return runner, state, task


def _decision(runner, action="request_replan", *, revision=1):
    run = SupervisorRun(
        f"obs-{revision}", revision,
        SupervisorDecision(
            action, "correct ownership", reason="failing bound test proves ownership is wrong",
            evidence=("src/app.py bound test failed",),
            evidence_paths=("src/app.py",),
            evidence_question="inspect the owner of this call",
            roles=("architecture",),
        ),
    )
    runner._record_supervisor_run(run, event="terminal_failure" if revision == 1 else "evidence_refreshed")
    return run


def test_supervisor_replan_preserves_failed_evaluation_and_consumes_once(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    task.evaluation = {"passed": False, "input_snapshot": "verified-snapshot", "summary": "old failure"}
    save_task(task)
    run = _decision(runner)

    assert runner._auto_recover(run) is True
    evaluation = load_task(task.id).evaluation
    assert evaluation["route"] == "replan"
    assert evaluation["replan_trigger"] == "supervisor_review"
    assert evaluation["input_snapshot"] == "verified-snapshot"
    assert state.phase == GoalPhase.REPAIR_PLAN.value
    assert state.supervision["recovery"]["consumed_at"]
    assert state.supervision["automatic_recovery"]["total"] == 1
    assert runner._auto_recover(run) is False


@pytest.mark.parametrize("reason", (
    StopReason.user_approval_required.value,
    StopReason.permission_wait.value,
    StopReason.budget_exhausted.value,
    StopReason.verification_contract_review.value,
))
def test_supervisor_does_not_resume_authority_or_contract_pause(tmp_path, monkeypatch, reason):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.stop_reason = reason
    run = _decision(runner)

    assert runner._auto_recover(run) is False
    assert state.phase == GoalPhase.PAUSED.value
    assert load_task(task.id).evaluation is None


def test_supervisor_recovery_needs_fresh_evidence_and_available_budget(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    run = _decision(runner)
    state.supervision["observation_revision"] = 2
    assert runner._auto_recover(run) is False
    assert load_task(task.id).evaluation is None

    state.supervision["observation_revision"] = 1
    state.budget_limits = {"llm_requests": 1}
    state.usage_ledger = {"llm_requests": 1}
    assert runner._auto_recover(run) is False
    assert state.phase == GoalPhase.PAUSED.value


def test_supervisor_never_replans_completed_or_passing_task(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    run = _decision(runner)
    task.evaluation = {"passed": True}
    save_task(task)

    assert runner._auto_recover(run) is False
    assert load_task(task.id).evaluation == {"passed": True}
    task.status = "completed"
    task.evaluation = {}
    save_task(task)
    assert runner._auto_recover(run) is False


def test_evidence_request_does_not_trigger_legacy_runner_discovery(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    run = _decision(runner, action="request_evidence")
    monkeypatch.setattr(runner, "_refresh_goal_evidence", lambda *_args, **_kwargs: pytest.fail("discovery must be supervisor-invoked"))

    assert runner._auto_recover(run) is False
    assert state.phase == GoalPhase.PAUSED.value
    assert load_task(task.id).evaluation is None


def test_supervisor_discovery_tool_counts_requests_and_stops_at_budget(tmp_path, monkeypatch):
    import json
    from harness.goal.discovery import DiscoverySupervisor

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def run(): pass\n", encoding="utf-8")
    state.budget_limits = {"llm_requests": 2}
    state.usage_ledger = {"llm_requests": 1}
    state.total_llm_rounds = 1
    manifest = SimpleNamespace(
        revision=2,
        jobs=(SimpleNamespace(id="architecture-2", llm_rounds=1, status="done", read_paths=("src/app.py",)),),
        evidence=(SimpleNamespace(source_job="architecture-2"),),
    )
    monkeypatch.setattr(DiscoverySupervisor, "run", lambda *_args, **_kwargs: manifest)
    tools = runner._supervisor_orchestration_tools({
        "event": "terminal_failure", "observation_id": "obs-1", "revision": 1,
    })
    assert json.loads(tools["start_discovery"](
        paths=["src/app.py"], question="find the failing owner", roles=["architecture"],
    ))["status"] == "written"
    assert "error" in tools["start_replan"](
        reason="wrong owner", evidence=["src/app.py failed"], affected_task_ids=[task.id],
    )
    assert state.usage_ledger["llm_requests"] == 2
    assert state.total_llm_rounds == 2
    assert state.phase == GoalPhase.PAUSED.value


def test_terminal_budget_pause_does_not_invoke_supervisor(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.stop_reason = StopReason.budget_exhausted.value
    reviewed = []
    monkeypatch.setattr(runner, "_review_supervisor_boundary", lambda *_args, **_kwargs: reviewed.append(True))

    assert runner._review_terminal_supervision() is None
    assert reviewed == []


def test_terminal_supervisor_retries_transient_outage_and_returns_recovery(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    calls = []
    failed = SupervisorRun("failed", 1, SupervisorDecision("watch", "outage", unavailable=True),
                           stop_reason="provider_error", retryable=True)
    recovered = SupervisorRun("recovered", 2, SupervisorDecision("retry", "resume checkpoint"))
    monkeypatch.setattr(runner._cancel_event, "wait", lambda *_: False)
    monkeypatch.setattr(runner, "_review_supervisor_boundary",
                        lambda *_args, **_kwargs: calls.append(True) or (failed if len(calls) == 1 else recovered))

    assert runner._review_terminal_supervision() is recovered
    assert len(calls) == 2
    assert runner._review_terminal_supervision() is None


def test_terminal_supervisor_outage_retry_is_bounded(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    calls = []
    failed = SupervisorRun("failed", 1, SupervisorDecision("watch", "outage", unavailable=True),
                           stop_reason="provider_error", retryable=True)
    monkeypatch.setattr(runner._cancel_event, "wait", lambda *_: False)
    monkeypatch.setattr(runner, "_review_supervisor_boundary", lambda *_args, **_kwargs: calls.append(True) or failed)

    assert runner._review_terminal_supervision() is failed
    assert len(calls) == 3
    assert runner._review_terminal_supervision() is None


@pytest.mark.parametrize("stop_reason,retryable", (("configuration_error", True), ("provider_error", False), ("cancelled", True)))
def test_terminal_supervisor_does_not_retry_nontransient_failures(tmp_path, monkeypatch, stop_reason, retryable):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    calls = []
    failed = SupervisorRun("failed", 1, SupervisorDecision("watch", "blocked", unavailable=True),
                           stop_reason=stop_reason, retryable=retryable)
    monkeypatch.setattr(runner, "_review_supervisor_boundary", lambda *_args, **_kwargs: calls.append(True) or failed)
    runner._review_terminal_supervision()
    assert len(calls) == 1


def test_supervisor_replan_tool_preserves_candidate_after_technical_failure(tmp_path, monkeypatch):
    import json

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    task.evaluation = {"passed": False, "route": "replan", "findings": [{"issue": "original ownership error"}]}
    save_task(task)
    state.execution_replan_checkpoint = {"failure_kind": "review_format", "candidate": {"tasks": ["saved"]}}

    def repair(current):
        assert load_task(task.id).evaluation == task.evaluation
        assert current.execution_replan_checkpoint["candidate"] == {"tasks": ["saved"]}
        runner.engine.transition(current, GoalPhase.SELECT_TASK, "execution_replan_accepted")

    monkeypatch.setattr(runner, "_repair_plan", repair)
    tools = runner._supervisor_orchestration_tools({"event": "terminal_failure", "observation_id": "obs-1", "revision": 1})
    result = json.loads(tools["start_replan"](reason="retry reviewer format", evidence=["candidate saved"], affected_task_ids=[task.id]))
    assert result["status"] == "written"


def test_saved_replan_format_failure_recovers_without_supervisor_then_stops(tmp_path, monkeypatch):
    from harness.goal.runner import EXECUTION_REPLAN_CHECKPOINT_VERSION

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.stop_reason = StopReason.execution_preflight_failed.value
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    state.execution_replan_checkpoint = {
        "version": EXECUTION_REPLAN_CHECKPOINT_VERSION,
        "goal_id": state.id, "task_id": task.id, "key": "same-candidate",
        "stage": "review", "failure_kind": "review_format", "retryable": True,
        "candidate": {"tasks": [{"name": "saved"}]},
    }
    unavailable = SupervisorRun("obs", 1, SupervisorDecision("watch", "offline", unavailable=True),
                                stop_reason="provider_error")

    for attempt in range(2):
        assert runner._retry_saved_replan_after_outage(unavailable) is True
        assert state.phase == GoalPhase.REPAIR_PLAN.value
        runner.engine.transition(state, GoalPhase.PAUSED, "reviewer_format_failure",
                                 stop_reason=StopReason.execution_preflight_failed.value)
        state.resume_phase = GoalPhase.REPAIR_PLAN.value
    assert runner._retry_saved_replan_after_outage(unavailable) is False
    assert state.supervision["automatic_recovery"]["checkpoint_failures"]["same-candidate:review_format"] == 2


@pytest.mark.parametrize("failure_kind,retryable", (("review_rejected", True), ("provider_error", False)))
def test_saved_replan_does_not_recover_unapproved_or_nontransient_failure(tmp_path, monkeypatch, failure_kind, retryable):
    from harness.goal.runner import EXECUTION_REPLAN_CHECKPOINT_VERSION

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.stop_reason = StopReason.execution_preflight_failed.value
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    state.execution_replan_checkpoint = {
        "version": EXECUTION_REPLAN_CHECKPOINT_VERSION, "goal_id": state.id,
        "task_id": task.id, "key": "candidate", "stage": "review",
        "failure_kind": failure_kind, "retryable": retryable,
        "candidate": {"tasks": [{"name": "saved"}]},
    }
    unavailable = SupervisorRun("obs", 1, SupervisorDecision("watch", "offline", unavailable=True))
    assert runner._retry_saved_replan_after_outage(unavailable) is False
    assert state.phase == GoalPhase.PAUSED.value


def test_goal_run_continues_saved_review_while_supervisor_is_offline(tmp_path, monkeypatch):
    import harness.goal.runner as runner_module

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.stop_reason = StopReason.execution_preflight_failed.value
    state.resume_phase = GoalPhase.REPAIR_PLAN.value
    state.execution_replan_checkpoint = {
        "version": runner_module.EXECUTION_REPLAN_CHECKPOINT_VERSION,
        "goal_id": state.id, "task_id": task.id, "key": "saved",
        "stage": "review", "failure_kind": "review_format", "retryable": True,
        "candidate": {"tasks": [{"name": "saved"}]},
    }
    phases = []

    def drive():
        phases.append(state.phase)
        if len(phases) == 2:
            state.status = "done"
            state.phase = "done"

    outage = SupervisorRun("obs", 1, SupervisorDecision("watch", "offline", unavailable=True))
    def review():
        state.supervision["terminal_boundary_revision"] = len(state.transition_log)
        return outage if state.status == "paused" else None

    monkeypatch.setattr(runner, "_start_supervisor", lambda: None)
    monkeypatch.setattr(runner, "_poll_supervisor", lambda: None)
    monkeypatch.setattr(runner, "_close_supervisor", lambda: None)
    monkeypatch.setattr(runner, "_drive", drive)
    monkeypatch.setattr(runner, "_review_terminal_supervision", review)
    monkeypatch.setattr(runner_module, "archive_goal", lambda *_: None)
    monkeypatch.setattr(runner_module, "release_goal_lease", lambda *_: None)
    runner.run()

    assert phases == ["paused", "repair_plan"]
    assert state.status == "done"


def test_supervisor_recovery_has_global_attempt_limit(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.supervision["automatic_recovery"] = {"total": 3, "failures": {}}
    run = _decision(runner)

    assert runner._auto_recover(run) is False
    assert state.phase == GoalPhase.PAUSED.value
    assert load_task(task.id).evaluation is None


def test_exhausted_execution_replan_retry_cannot_trigger_runner_rediscovery(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    for relative in ("harness/settings.py", "harness/tasks.py", "harness/rag/config.py", "harness/rag/tools.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# evidence\n", encoding="utf-8")
    state.execution_replan_checkpoint = {
        "goal_id": state.id,
        "task_id": task.id,
        "failure_count": 2,
        "review": {
            "approved": False,
            "summary": "ownership is ambiguous",
            "findings": [{"issue": "migration boundaries overlap", "evidence": "harness/rag/config.py"}],
        },
        "candidate": {
            "tasks": [{
                "primary_write": ["harness/settings.py", "harness/tasks.py"],
                "read_envelope": ["harness/rag/config.py", "harness/rag/tools.py"],
            }],
        },
    }
    retry = SupervisorRun(
        "obs-1", 1,
        SupervisorDecision(
            "retry", "retrying the saved candidate", reason="the checkpoint is exhausted",
            evidence=("repeated structural reviewer finding",),
        ),
    )
    runner._record_supervisor_run(retry, event="terminal_failure")
    assert "recovery" not in state.supervision
    monkeypatch.setattr(runner, "_refresh_goal_evidence", lambda *_args, **_kwargs: pytest.fail("discovery must be supervisor-invoked"))
    assert runner._auto_recover(retry) is False
    assert state.phase == GoalPhase.PAUSED.value


def test_replan_checkpoint_changes_when_evidence_or_failure_changes(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    params = {
        "selected_ids": {task.id}, "replacement_scope": ({"task_id": task.id},),
        "execution_workspace_paths": ("src/app.py",),
    }
    first = runner._execution_replan_checkpoint_key(state, task, **params, evidence_revision=1, failure_signature="old")
    refreshed = runner._execution_replan_checkpoint_key(state, task, **params, evidence_revision=2, failure_signature="old")
    new_failure = runner._execution_replan_checkpoint_key(state, task, **params, evidence_revision=1, failure_signature="new")

    assert len({first, refreshed, new_failure}) == 3


def test_supervisor_tools_persist_discovery_and_run_reviewed_replan_in_session(tmp_path, monkeypatch):
    import json
    from harness.goal import discovery_store

    runner, state, task = _paused_task(tmp_path, monkeypatch)
    state.goal_contract = {"language": "en"}
    observation = {"event": "terminal_failure", "observation_id": "obs-live", "revision": 1}
    calls = []

    def refresh(paths, *, question, roles):
        calls.append((paths, question, roles))
        state.supervision["evidence_revision"] = 2
        return True

    monkeypatch.setattr(runner, "_refresh_goal_evidence", refresh)
    monkeypatch.setattr(discovery_store, "load_manifest", lambda *_: {
        "revision": 2, "evidence": [{"source_job": "architecture-2", "path": "src/app.py", "claim": "owner mismatch"}],
    })

    def repair(current):
        assert current.phase == GoalPhase.REPAIR_PLAN.value
        assert load_task(task.id).evaluation["replan_trigger"] == "supervisor_review"
        current.planning_review = {"execution_replan": {"approved": True}}
        runner.engine.transition(current, GoalPhase.SELECT_TASK, "execution_replan_accepted")

    monkeypatch.setattr(runner, "_repair_plan", repair)
    tools = runner._supervisor_orchestration_tools(observation)
    discovery = json.loads(tools["start_discovery"](
        paths=["src/app.py"], question="check ownership", roles=["architecture"],
    ))
    assert discovery["evidence"][0]["claim"] == "owner mismatch"
    assert calls == [(('src/app.py',), "check ownership", ("architecture",))]

    result = json.loads(tools["start_replan"](
        reason="bound test proves owner mismatch", evidence=["src/app.py: owner mismatch"],
        affected_task_ids=[task.id],
    ))
    assert result["status"] == "written"
    assert state.phase == GoalPhase.SELECT_TASK.value
    assert state.supervision["automatic_recovery"]["total"] == 1
    assert "error" in tools["start_replan"](
        reason="again", evidence=["duplicate"], affected_task_ids=[task.id],
    )


def test_supervisor_domain_tools_reject_stale_or_authority_pauses(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    tools = runner._supervisor_orchestration_tools({
        "event": "terminal_failure", "observation_id": "obs", "revision": 1,
    })
    state.stop_reason = StopReason.permission_wait.value
    assert "error" in tools["start_discovery"](paths=["src/app.py"], question="inspect", roles=["tests"])
    assert "error" in tools["start_replan"](reason="wrong", evidence=["failure"], affected_task_ids=[task.id])
    state.stop_reason = StopReason.task_blocked.value
    state.transition_log.append({"from": "repair_plan", "to": "paused", "reason": "new_failure"})
    assert "error" in tools["start_replan"](reason="wrong", evidence=["failure"], affected_task_ids=[task.id])


def test_orchestrated_discovery_is_not_repeated_by_legacy_auto_recovery(tmp_path, monkeypatch):
    runner, state, task = _paused_task(tmp_path, monkeypatch)
    observed = {"event": "terminal_failure", "observation_id": "obs-1", "revision": 1}
    monkeypatch.setattr(runner, "_refresh_goal_evidence", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("harness.goal.discovery_store.load_manifest", lambda *_: {"revision": 2, "evidence": []})
    tools = runner._supervisor_orchestration_tools(observed)
    tools["start_discovery"](paths=["src/app.py"], question="check owner", roles=["tests"])
    run = _decision(runner, action="request_evidence")
    monkeypatch.setattr(runner, "_refresh_goal_evidence", lambda *_args, **_kwargs: pytest.fail("discovery repeated"))
    assert runner._auto_recover(run) is False
