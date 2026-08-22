"""Read-only global Goal supervisor contracts."""

from __future__ import annotations

import threading
import time

from harness.goal.coordinator import (
    ParallelGoalSupervisor,
    SupervisorDecision,
    SupervisorRun,
    analyze_goal_observation,
    parse_supervisor_decision,
)


def test_supervisor_parser_finds_one_balanced_object_inside_agent_text():
    decision = parse_supervisor_decision(
        'prefix {not json} then '
        '{"action":"retry","summary":"transient failure","next_step":"retry checkpoint"} '
        'and trailing {noise'
    )

    assert decision.action == "retry"
    assert decision.summary == "transient failure"
    assert not decision.unavailable


def test_supervisor_parser_rejects_unknown_actions():
    decision = parse_supervisor_decision('{"action":"grant_everything","summary":"unsafe"}')

    assert decision.action == "watch"
    assert decision.unavailable
    assert "unsupported" in decision.error


def test_supervisor_repairs_invalid_json_once():
    responses = iter(("not json", '{"action":"watch","summary":"recovered"}'))
    calls: list[str] = []

    def fake_runner(**kwargs):
        calls.append(kwargs["description"])
        kwargs["stats"].llm_rounds = 1
        return next(responses)

    run = analyze_goal_observation(
        {"observation_id": "obs-1", "revision": 2, "event": "verify"},
        cwd=".",
        deadline=time.monotonic() + 10,
        runner=fake_runner,
    )

    assert run.decision.summary == "recovered"
    assert run.llm_rounds == 2
    assert len(calls) == 2


def test_supervisor_provider_failure_degrades_to_watch():
    def unavailable(**kwargs):
        kwargs["stats"].stop_reason = "provider_error"
        return "provider unavailable"

    run = analyze_goal_observation(
        {"observation_id": "obs-1", "revision": 1, "event": "act"},
        cwd=".",
        deadline=time.monotonic() + 10,
        runner=unavailable,
    )

    assert run.decision.action == "watch"
    assert run.decision.unavailable
    assert "provider_error" in run.decision.error


def test_parallel_supervisor_coalesces_pending_observations_to_latest():
    first_started = threading.Event()
    release_first = threading.Event()
    analyzed: list[str] = []

    def analyzer(observation, **_kwargs):
        event = str(observation["event"])
        analyzed.append(event)
        if event == "first":
            first_started.set()
            release_first.wait(timeout=2)
        return SupervisorRun(
            str(observation["observation_id"]),
            int(observation["revision"]),
            SupervisorDecision("watch", event),
        )

    supervisor = ParallelGoalSupervisor(
        cwd=".",
        operation_timeout_seconds=5,
        analyzer=analyzer,
    )
    try:
        supervisor.observe({"event": "first", "revision": 1})
        assert first_started.wait(timeout=1)
        supervisor.observe({"event": "superseded", "revision": 2})
        supervisor.observe({"event": "latest", "revision": 3})
        release_first.set()
        deadline = time.monotonic() + 2
        results = []
        while time.monotonic() < deadline and len(results) < 2:
            results.extend(supervisor.poll())
            time.sleep(0.01)
    finally:
        supervisor.close()

    assert analyzed == ["first", "latest"]
    assert [item.decision.summary for item in results] == ["first", "latest"]


def test_boundary_review_preserves_an_earlier_parallel_result_for_polling():
    first_done = threading.Event()

    def analyzer(observation, **_kwargs):
        result = SupervisorRun(
            str(observation["observation_id"]),
            int(observation["revision"]),
            SupervisorDecision("watch", str(observation["event"])),
        )
        if observation["event"] == "parallel":
            first_done.set()
        return result

    supervisor = ParallelGoalSupervisor(
        cwd=".",
        operation_timeout_seconds=5,
        analyzer=analyzer,
    )
    try:
        supervisor.observe({"event": "parallel", "revision": 1})
        assert first_done.wait(timeout=1)
        boundary = supervisor.review({"event": "boundary", "revision": 2})
        earlier = supervisor.poll()
    finally:
        supervisor.close()

    assert boundary.decision.summary == "boundary"
    assert [item.decision.summary for item in earlier] == ["parallel"]


def test_boundary_review_bypasses_a_blocked_parallel_observation():
    parallel_started = threading.Event()
    release_parallel = threading.Event()

    def analyzer(observation, **_kwargs):
        event = str(observation["event"])
        if event == "parallel":
            parallel_started.set()
            release_parallel.wait(timeout=2)
        return SupervisorRun(
            str(observation["observation_id"]),
            int(observation["revision"]),
            SupervisorDecision("retry", event),
        )

    supervisor = ParallelGoalSupervisor(
        cwd=".",
        operation_timeout_seconds=5,
        analyzer=analyzer,
    )
    try:
        supervisor.observe({"event": "parallel", "revision": 1})
        assert parallel_started.wait(timeout=1)
        started = time.monotonic()
        boundary = supervisor.review({"event": "terminal_failure", "revision": 2})
    finally:
        release_parallel.set()
        supervisor.close()

    assert boundary.decision.summary == "terminal_failure"
    assert time.monotonic() - started < 0.5


def test_terminal_permission_boundary_is_not_analyzed_twice(tmp_path, monkeypatch):
    from harness.goal.models import GoalPhase, GoalState, GoalStatus
    from harness.goal.runner import GoalRunner

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.phase = GoalPhase.PAUSED.value
    state.status = GoalStatus.PAUSED.value
    state.stop_reason = "permission_wait"
    state.transition_log = [{"from": "act", "to": "paused", "reason": "goal_permission_required"}]
    state.supervision = {"terminal_boundary_revision": 1}
    runner = GoalRunner(state=state, history=[], context={}, binding=None)
    reviewed: list[str] = []
    monkeypatch.setattr(
        runner,
        "_review_supervisor_boundary",
        lambda event, **kwargs: reviewed.append(event),
    )

    runner._review_terminal_supervision()

    assert reviewed == []


def test_stale_parallel_result_stays_in_history_without_replacing_latest(tmp_path, monkeypatch):
    import harness.goal.runner as runner_mod
    from harness.goal.models import GoalState
    from harness.goal.runner import GoalRunner

    state = GoalState.new(target="x", verification="pytest -q", workspace=str(tmp_path))
    state.supervision = {
        "status": "attention",
        "observation_revision": 2,
        "latest": {
            "action": "redirect",
            "summary": "current boundary decision",
            "observation_id": "obs-2",
            "revision": 2,
        },
        "history": [],
    }
    monkeypatch.setattr(runner_mod, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_mod, "_emit", lambda *args, **kwargs: None)
    runner = GoalRunner(state=state, history=[], context={}, binding=None)

    runner._record_supervisor_run(
        SupervisorRun(
            "obs-1",
            1,
            SupervisorDecision("continue", "older routine observation"),
        ),
        event="parallel_observation",
    )

    assert state.supervision["status"] == "attention"
    assert state.supervision["latest"]["observation_id"] == "obs-2"
    assert state.supervision["history"][-1]["stale"] is True
