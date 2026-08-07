"""G-series: /goal autonomous execution state machine (L6).

Deterministic, zero-LLM. All agent / verification / evaluation / clean calls
are mocked; goals run against temp workspaces. The real workspace's
``.project/goal.json``, ``.tasks/archive/`` and ``.features`` are never
touched (guarded by the final pollution case).

Test plan (docs/goal-mode-mvp-spec.md §12):

- G001  legal transitions / illegal -> GoalTransitionError
- G002  initialize creates exactly 1 task + 1 feature (WIP=1)
- G003  verification pass + clean ok -> DONE, task completed
- G004  verification fail -> ACT retries, attempts accumulate
- G005  max consecutive failures fuse
- G006  max duration fuse
- G007  no-progress fuse
- G008  pause / resume with durable state
- G009  cancel
- G010  process-restart recovery
- G011  WIP=1 machine constraint
- G012  stale passing feature -> ACT (reopen), never DONE
- G013  clean failure -> ACT, cap -> FAILED
- G014  evaluator advisory (runs once, never gates state)
- G015  /goal command parsing
- G016  TUI busy controls (status/pause/cancel instant, normal msg rejected)
- G017  atomic store (os.replace failure keeps old file)
- G018  corrupt state file -> reported, never overwritten
- G019  workspace isolation
- G020  non-interactive permission ask -> PAUSED/permission_wait
- G021  model switch during goal is instant and does not cancel
- G022  agent self-reported DONE does not complete the goal
- G023  tasks dir follows the active workspace (/open safety)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent

#: Real-workspace pollution sentinels captured at import time.
_REAL_GOAL_FILE = ROOT / ".project" / "goal.json"
_REAL_TASKS_ARCHIVE = ROOT / ".tasks" / "archive"
_REAL_FEATURES = ROOT / ".features"
_IMPORT_POLLUTION = (
    _REAL_GOAL_FILE.read_bytes() if _REAL_GOAL_FILE.exists() else None,
    len(list(_REAL_TASKS_ARCHIVE.glob("task_*.json"))) if _REAL_TASKS_ARCHIVE.exists() else 0,
    len(list(_REAL_FEATURES.glob("feat_*.json"))) if _REAL_FEATURES.exists() else 0,
)


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


def _git_init(ws: Path) -> None:
    """Init a git repo WITH a resolvable HEAD (goal start precondition)."""
    subprocess.run(["git", "init", "-q", str(ws)], check=False, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(ws), check=False, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=False, capture_output=True)
    marker = ws / ".gitkeep"
    if not marker.exists():
        marker.write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(ws), check=False, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(ws), check=False, capture_output=True)


@contextmanager
def _active_workspace(ws: Path):
    """Temporarily switch the process workspace (restored on exit)."""
    from harness import settings as settings_mod

    before = settings_mod.get_workdir()
    settings_mod.switch_workspace(ws)
    try:
        yield
    finally:
        settings_mod.switch_workspace(before)


def _new_state(ws: Path, **limits) -> "object":
    from harness import settings as settings_mod
    from harness.goal.models import GoalState

    return GoalState.new(
        target="fix the pagination edge",
        verification="python check.py",
        workspace=str(ws),
        workspace_generation=settings_mod.workspace_generation(),
        **limits,
    )


def _write_code(ws: Path, n: int) -> None:
    (ws / f"work_{n}.py").write_text(f"x = {n}", encoding="utf-8")


def _mock_agent_writes(ws: Path, calls: dict):
    def fake_agent_loop(messages, context, **kw):
        calls["n"] += 1
        _write_code(ws, calls["n"])
        messages.append({"role": "assistant", "content": f"attempt {calls['n']} done"})
        kw["stats"].llm_rounds = 1
        return False

    return fake_agent_loop


def _mock_verify_pass(feature_id, workspace=None, **kw):
    from harness.features import get_feature, verify_feature
    from harness.features.schema import VerificationEvidence

    get_feature(feature_id, workspace)
    return verify_feature(
        feature_id,
        True,
        VerificationEvidence(
            command="python check.py",
            exit_code=0,
            verified_by="harness",
        ),
        workspace=workspace,
    )


def _mock_verify_fail(feature_id, workspace=None, **kw):
    from harness.features import verify_feature

    return verify_feature(
        feature_id,
        False,
        error="verification failed with exit code 1",
        workspace=workspace,
    )


def _single_plan(*args, **kwargs):
    """Zero-LLM planner stub: fall back to a single whole-goal feature."""
    from harness.goal.planner import FeaturePlan

    target = kwargs.get("target") or (args[0] if args else "goal")
    return [
        FeaturePlan(
            name=(str(target).strip()[:40] or "goal"),
            behavior=str(target),
            verification="",
            depends_on=(),
        )
    ]


def _multi_plan(*args, **kwargs):
    """Zero-LLM planner stub: a 3-feature chain with dependencies."""
    from harness.goal.planner import FeaturePlan

    return [
        FeaturePlan(name="feat-a", behavior="first feature", verification="", depends_on=()),
        FeaturePlan(name="feat-b", behavior="second feature", verification="", depends_on=("feat-a",)),
        FeaturePlan(name="feat-c", behavior="third feature", verification="", depends_on=("feat-b",)),
    ]


# --- G001: legal transitions --------------------------------------------------

def case_g001_legal_transitions() -> None:
    """Normal transitions pass; illegal ones raise GoalTransitionError."""
    from harness.goal.engine import GoalEngine, GoalTransitionError
    from harness.goal.models import GoalPhase

    tmp, ws = _tmp_workspace()
    try:
        engine = GoalEngine()
        state = _new_state(ws)
        state = engine.initialize(state)
        assert state.phase == GoalPhase.SELECT_FEATURE.value
        assert state.status == "running"
        assert len(state.transition_log) == 1
        assert state.transition_log[0]["from"] == "initialize"
        assert state.transition_log[0]["to"] == "select_feature"

        state = engine.transition(state, GoalPhase.CLAIM, "feature_not_started")
        state = engine.transition(state, GoalPhase.ACT, "claimed")
        state = engine.transition(state, GoalPhase.VERIFY, "agent_loop_finished")
        state = engine.transition(state, GoalPhase.CLEAN_CHECK, "verification_passed")
        state = engine.transition(state, GoalPhase.DONE, "clean_checks_passed")
        assert state.status == "done"
        assert state.completed_at is not None

        for target in (GoalPhase.ACT, GoalPhase.VERIFY, GoalPhase.DONE, GoalPhase.FAILED, GoalPhase.PAUSED):
            try:
                engine.transition(state, target, "illegal")
            except GoalTransitionError:
                pass
            else:
                raise AssertionError(f"DONE -> {target} must raise GoalTransitionError")

        fresh = _new_state(ws)
        try:
            engine.transition(fresh, GoalPhase.ACT, "skip")
        except GoalTransitionError:
            pass
        else:
            raise AssertionError("initialize -> act must raise GoalTransitionError")

        paused = _new_state(ws)
        engine.initialize(paused)
        engine.transition(paused, GoalPhase.CLAIM, "feature_not_started")
        engine.transition(paused, GoalPhase.ACT, "claimed")
        engine.transition(paused, GoalPhase.PAUSED, "user_pause")
        resumed = engine.transition(paused, GoalPhase.SELECT_FEATURE, "resumed")
        assert resumed.status == "running"

        big = _new_state(ws)
        big = engine.initialize(big)
        big = engine.transition(big, GoalPhase.ACT, "start")
        for i in range(150):
            big = engine.transition(big, GoalPhase.VERIFY, f"loop-{i}")
            big = engine.transition(big, GoalPhase.ACT, f"loop-{i}")
        assert len(big.transition_log) <= 100
    finally:
        tmp.cleanup()


# --- G002 / G003: initialize graph + pass-to-done -----------------------------

def _drive_goal(state, ws, *, verify=_mock_verify_pass, agent=None, clean=None):
    from harness.goal import runner

    calls = {"n": 0}
    fake_agent = agent or _mock_agent_writes(ws, calls)
    patches = [
        mock.patch.object(runner, "agent_loop", fake_agent),
        mock.patch.object(runner, "verify_feature_command", verify),
        mock.patch.object(runner, "plan_features", _single_plan),
    ]
    if clean is not None:
        patches.append(mock.patch.object(runner, "run_clean_check", clean))
    with _MultiPatch(patches):
        r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
        r.run()
    return calls


class _MultiPatch:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


def case_g002_initialize_creates_graph() -> None:
    """INITIALIZE creates exactly 1 task + 1 feature with bidirectional ids."""
    import harness.features as feat_mod
    from harness.goal import runner
    from harness.tasks import list_archived_tasks, list_tasks

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    (ws / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=1, max_rounds_per_attempt=2)
        claimed = []

        def tracking_claim(feature_id, workspace=None):
            claimed.append(feature_id)
            from harness.features.state import claim_feature as _real_claim

            return _real_claim(feature_id, workspace=workspace)

        def fake_agent(messages, context, **kw):
            _write_code(ws, 1)
            messages.append({"role": "assistant", "content": "done"})
            kw["stats"].llm_rounds = 1
            kw["stats"].stop_reason = "completed"
            assert kw["disabled_tools"] >= {
                "create_feature", "claim_feature", "verify_feature",
                "evaluate_feature", "complete_task", "clear_tasks",
            }
            assert kw["max_rounds"] == state.max_rounds_per_attempt
            return False

        with _MultiPatch([
            mock.patch.object(runner, "agent_loop", fake_agent),
            mock.patch.object(runner, "verify_feature_command", _mock_verify_pass),
            mock.patch.object(runner, "plan_features", _single_plan),
            mock.patch.object(feat_mod, "claim_feature", tracking_claim),
        ]):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()

        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        assert state.task_id and state.feature_id

        # Exactly one task and one feature were created by the goal.
        active = list_tasks()
        archived = list_archived_tasks()
        assert len(active) == 0, f"goal must archive its task, active board: {active}"
        assert len(archived) == 1, f"expected exactly 1 archived task, got {len(archived)}"
        assert archived[0].id == state.task_id
        assert archived[0].feature_ids == [state.feature_id]

        from harness.features import list_features

        features = list_features(workspace=ws)
        assert len(features) == 1, f"expected exactly 1 feature, got {len(features)}"
        assert features[0].id == state.feature_id
        assert features[0].task_id == state.task_id

        # WIP=1: the only feature ever claimed is the goal's own.
        assert claimed == [state.feature_id], f"claims leaked: {claimed}"

        # ACT used a restricted tool pool + accumulated LoopStats rounds.
        assert state.total_llm_rounds == 1
    tmp.cleanup()


def case_g003_verification_pass_done() -> None:
    """VERIFY passing + clean ok -> DONE; the linked task is completed."""
    from harness.goal import runner
    from harness.tasks import load_task

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    (ws / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    with _active_workspace(ws):
        state = _new_state(ws)
        _drive_goal(state, ws)
        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        assert state.phase == "done"
        assert state.completed_at is not None

        task = load_task(state.task_id)
        assert task.status == "completed"
        assert task.feature_ids == [state.feature_id]

        from harness.features import get_feature

        feature = get_feature(state.feature_id, workspace=ws)
        assert feature.state == "passing"
        assert feature.evidence[-1]["exit_code"] == 0

        reasons = [t["reason"] for t in state.transition_log]
        assert "verification_passed" in reasons
        assert "clean_checks_passed" in reasons
    tmp.cleanup()


# --- G004 / G005: failure retries + fuses -------------------------------------

def case_g004_verification_fail_retries() -> None:
    """failing -> ACT with attempts accumulating, never past the caps."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=3, max_consecutive_failures=3)
        calls = _drive_goal(state, ws, verify=_mock_verify_fail)
        assert state.status == "failed"
        assert state.stop_reason in ("max_attempts", "max_consecutive_failures")
        assert calls["n"] == 3, f"agent must stop at the cap, called {calls['n']}x"
        assert state.attempts == 3
        # Retried at least once before giving up.
        reasons = [t["reason"] for t in state.transition_log]
        assert reasons.count("verification_failed") >= 1
    tmp.cleanup()


def case_g005_max_failures_fuse() -> None:
    """max_consecutive_failures fuse: FAILED with the right stop_reason."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=10, max_consecutive_failures=3)
        calls = _drive_goal(state, ws, verify=_mock_verify_fail)
        assert state.status == "failed"
        assert state.stop_reason == "max_consecutive_failures"
        assert calls["n"] == 3, "no further agent calls after the fuse"
    tmp.cleanup()


def case_g006_max_duration_fuse() -> None:
    """A goal that overruns max_duration_seconds fails with max_duration."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_duration_seconds=1)

        def slow_agent(messages, context, **kw):
            _write_code(ws, 1)
            time.sleep(1.5)
            messages.append({"role": "assistant", "content": "slow work"})
            kw["stats"].llm_rounds = 1
            return False

        _drive_goal(state, ws, agent=slow_agent, verify=_mock_verify_pass)
        assert state.status == "failed"
        assert state.stop_reason == "max_duration"
    tmp.cleanup()


def case_g007_no_progress_fuse() -> None:
    """Two consecutive ACTs with no code/error change -> FAILED/no_progress."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=10, max_consecutive_failures=10)
        calls = {"n": 0}

        def inert_agent(messages, context, **kw):
            calls["n"] += 1
            messages.append({"role": "assistant", "content": "tried"})
            kw["stats"].llm_rounds = 1
            return False

        _drive_goal(state, ws, agent=inert_agent, verify=_mock_verify_fail)
        assert state.status == "failed"
        assert state.stop_reason == "no_progress"
        assert calls["n"] == 2, f"expected 2 attempts before fuse, got {calls['n']}"
    tmp.cleanup()


# --- G008 / G009: pause / cancel ----------------------------------------------

def case_g008_pause_resume() -> None:
    """pause mid-ACT -> PAUSED (durable); resume -> SELECT_FEATURE."""
    from harness.goal import runner, store
    from harness.goal.models import GoalPhase

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        started = threading.Event()
        release = threading.Event()

        def blocking_agent(messages, context, **kw):
            started.set()
            release.wait(10)
            _write_code(ws, 1)
            messages.append({"role": "assistant", "content": "partial"})
            kw["stats"].llm_rounds = 2
            return True

        with mock.patch.object(runner, "agent_loop", blocking_agent), \
             mock.patch.object(runner, "plan_features", _single_plan):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.start()
            assert started.wait(5), "ACT did not start"
            paused = r.request_pause()
            assert paused.status == "pausing"
            release.set()
            r.join(5)

        assert state.status == "paused"
        assert state.phase == GoalPhase.PAUSED.value
        assert state.attempts == 1
        loaded = store.load_goal(workspace=ws)
        assert loaded is not None and loaded.status == "paused"

        # Resume returns to SELECT_FEATURE with a running status.
        resumed = runner.resume_goal(history=[], context={}, binding=None)
        assert resumed.status == "running"
        assert resumed.phase == GoalPhase.SELECT_FEATURE.value
        assert any(t["reason"] == "resumed" for t in resumed.transition_log)

        store.clear_goal_for_test(ws)
    tmp.cleanup()


def case_g009_cancel() -> None:
    """cancel -> CANCELLED (terminal); no further phases run."""
    from harness.goal import runner, store

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        started = threading.Event()
        release = threading.Event()

        def blocking_agent(messages, context, **kw):
            started.set()
            release.wait(10)
            messages.append({"role": "assistant", "content": "partial"})
            kw["stats"].llm_rounds = 1
            return True

        with mock.patch.object(runner, "agent_loop", blocking_agent), \
             mock.patch.object(runner, "plan_features", _single_plan):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.start()
            assert started.wait(5)
            r.request_cancel()
            release.set()
            r.join(5)

        assert state.status == "cancelled"
        assert state.stop_reason == "cancelled_by_user"
        # Terminal state is archived but the slot is kept for status.
        hist = store.history_dir(ws)
        assert list(hist.glob(f"{state.id}.json")), "terminal goal must be archived"
        slot = store.load_goal(workspace=ws)
        assert slot is not None and slot.status == "cancelled"
    tmp.cleanup()


# --- G010: process restart ----------------------------------------------------

def case_g010_process_restart_recovery() -> None:
    """A running goal on disk loads as paused/process_restarted."""
    from harness.goal import store
    from harness.goal.models import GoalPhase, GoalStatus, StopReason

    tmp, ws = _tmp_workspace()
    try:
        state = _new_state(ws)
        state.status = GoalStatus.RUNNING.value
        state.phase = GoalPhase.ACT.value
        store.save_goal(state)

        loaded = store.load_goal(workspace=ws)
        assert loaded is not None
        assert loaded.status == GoalStatus.PAUSED.value
        assert loaded.phase == GoalPhase.PAUSED.value
        assert loaded.stop_reason == StopReason.process_restarted.value

        state.status = GoalStatus.PAUSING.value
        store.save_goal(state)
        loaded = store.load_goal(workspace=ws)
        assert loaded.status == GoalStatus.PAUSED.value

        loaded.status = GoalStatus.PAUSED.value
        store.save_goal(loaded)
        again = store.load_goal(workspace=ws)
        assert again.status == GoalStatus.PAUSED.value
    finally:
        tmp.cleanup()


# --- G011: WIP=1 --------------------------------------------------------------

def case_g011_wip_one() -> None:
    """A pre-existing active feature is never touched; only the goal's own
    feature is claimed (machine constraint)."""
    import harness.features as feat_mod
    from harness.features import claim_feature, create_feature
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        other = create_feature("other-work", "unrelated", "python check.py", workspace=ws)
        claim_feature(other.id, workspace=ws)
        state = _new_state(ws)
        claimed = []

        def tracking_claim(feature_id, workspace=None, **kw):
            claimed.append(feature_id)
            from harness.features.state import claim_feature as _real_claim

            return _real_claim(feature_id, workspace=workspace)

        with _MultiPatch([
            mock.patch.object(feat_mod, "claim_feature", tracking_claim),
            mock.patch.object(
                runner,
                "agent_loop",
                _mock_agent_writes(ws, {"n": 0}),
            ),
            mock.patch.object(runner, "verify_feature_command", _mock_verify_pass),
            mock.patch.object(runner, "plan_features", _single_plan),
        ]):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()

        from harness.features import get_feature, list_features

        features = list_features(workspace=ws)
        assert len(features) == 2  # the pre-existing one + the goal's own
        assert claimed == [state.feature_id], f"WIP=1 violated: {claimed}"
        untouched = get_feature(other.id, workspace=ws)
        assert untouched.state == "active", "pre-existing feature must stay untouched"
    tmp.cleanup()


# --- G012: stale passing ------------------------------------------------------

def case_g012_stale_passing() -> None:
    """A passing-but-stale feature is reopened and ACTed, never DONE directly."""
    from harness.features import create_feature, feature_is_stale, get_feature
    from harness.goal import runner
    from harness.tasks import create_task
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    (ws / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
    (ws / "app.py").write_text("x = 0", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws), check=False, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(ws), check=False, capture_output=True)
    with _active_workspace(ws):
        feat = create_feature("pagination", "behavior", "python check.py", workspace=ws)
        feat = verify_feature_command(feat.id, workspace=ws)  # real verification
        assert feat.state == "passing"
        _write_code(ws, 99)  # code changes after verification -> stale
        assert feature_is_stale(get_feature(feat.id, workspace=ws))

        task = create_task(subject="goal task", description="x")
        state = _new_state(ws)
        state.task_id = task.id
        state.feature_id = feat.id
        state.phase = "select_feature"

        _drive_goal(state, ws, verify=_mock_verify_pass)
        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        reasons = [(t["from"], t["to"], t["reason"]) for t in state.transition_log]
        assert ("select_feature", "act", "reopen_stale") in reasons, reasons
        assert not any(r[2] == "already_passing" for r in reasons)
        final = get_feature(feat.id, workspace=ws)
        assert final.state == "passing"
    tmp.cleanup()


# --- G013: clean failure ------------------------------------------------------

def case_g013_clean_failure() -> None:
    """Clean enforce failure -> ACT retry; cap -> FAILED/clean_check_failed."""
    from harness.clean import CleanCheck, CleanReport
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=3, max_consecutive_failures=10)

        def failing_clean(workspace=None, mode=None):
            return CleanReport(
                mode="enforce",
                checks=[CleanCheck("temp_artifacts", "no leftover temp files", ok=False, detail="junk.tmp")],
            )

        calls = _drive_goal(state, ws, verify=_mock_verify_pass, clean=failing_clean)
        assert state.status == "failed"
        assert state.stop_reason == "clean_check_failed"
        assert calls["n"] == 3, "clean failure must trigger ACT retries up to the cap"
        reasons = [t["reason"] for t in state.transition_log]
        assert "clean_check_failed" in reasons
    tmp.cleanup()


# --- G014: evaluator advisory -------------------------------------------------

def case_g014_evaluator_advisory() -> None:
    """evaluation_required runs the evaluator exactly once; findings never
    change feature state."""
    from harness.features import get_feature, record_evaluation
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, evaluation_required=True)
        eval_calls = {"n": 0}

        def fake_eval(feature_id, workspace=None, **kw):
            eval_calls["n"] += 1
            return record_evaluation(
                feature_id,
                {
                    "passed": False,
                    "summary": "edge case uncovered",
                    "findings": [{"severity": "low", "message": "off-by-one on page 2"}],
                    "evaluated_by": "mimo-v2.5-pro",
                    "evaluated_at": 0,
                },
                workspace=workspace,
            )

        with _MultiPatch([
            mock.patch.object(runner, "agent_loop", _mock_agent_writes(ws, {"n": 0})),
            mock.patch.object(runner, "plan_features", _single_plan),
            mock.patch.object(runner, "verify_feature_command", _mock_verify_pass),
            mock.patch.object(runner, "run_evaluation", fake_eval),
        ]):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()

        assert state.status == "done"
        assert eval_calls["n"] == 1, "evaluator must run exactly once"
        feature = get_feature(state.feature_id, workspace=ws)
        assert feature.state == "passing", "evaluator findings must not change feature state"
        assert feature.evaluation is not None
        assert feature.evaluation["passed"] is False
        assert len(feature.evaluation["findings"]) == 1
    tmp.cleanup()


# --- G015: command parsing ----------------------------------------------------

def case_g015_command_parsing() -> None:
    """/goal start/status/pause/resume/cancel parse correctly (incl. errors)."""
    from harness.goal.commands import parse_goal_command, parse_goal_subcommand

    # Subcommand classification.
    assert parse_goal_subcommand("/goal status") == "status"
    assert parse_goal_subcommand("/goal pause") == "pause"
    assert parse_goal_subcommand("/goal resume") == "resume"
    assert parse_goal_subcommand("/goal cancel") == "cancel"
    assert parse_goal_subcommand("/goal --verify pytest -q -- fix") == "start"
    assert parse_goal_subcommand("/goal") == "usage"
    assert parse_goal_subcommand("hello") is None

    # Full start command with limits.
    cmd = parse_goal_command('/goal --verify "pytest -q" --max-rounds 20 --timeout 1800 --max-failures 3 -- 修复分页边界')
    assert cmd["action"] == "start"
    assert cmd["verify"] == "pytest -q"
    assert cmd["limits"] == {"max_rounds": 20, "timeout": 1800, "max_failures": 3}
    assert cmd["target"] == "修复分页边界"

    # Everything after `--` is target text, even flag-like tokens.
    cmd = parse_goal_command('/goal --verify "pytest -q" -- --weird --max-rounds 5')
    assert cmd["target"] == "--weird --max-rounds 5"

    # Missing pieces are usage errors.
    assert parse_goal_command("/goal")["action"] == "usage"
    assert parse_goal_command("/goal 修复分页")["action"] == "usage"  # bare target: MVP refuses
    assert parse_goal_command('/goal --verify "pytest -q"')["action"] == "usage"  # no `--`
    assert parse_goal_command("/goal -- 修复")["action"] == "usage"  # no verify
    assert parse_goal_command("/goal status extra")["action"] == "usage"
    assert parse_goal_command('/goal --verify "pytest -q" --timeout abc -- x')["action"] == "usage"
    assert parse_goal_command('/goal --verify "pytest -q" --timeout 0 -- x')["action"] == "usage"
    assert parse_goal_command('/goal --verify "pytest -q" --unknown -- x')["action"] == "usage"
    assert parse_goal_command('/goal --verify "pytest -q" -- ' )["action"] == "usage"  # empty target

    # Optional limits map to GoalRequest fields.
    cmd = parse_goal_command('/goal --verify "pytest -q" --max-attempts 5 -- x')
    assert cmd["limits"] == {"max_attempts": 5}


# --- G016: TUI busy controls --------------------------------------------------

def case_g016_tui_busy_controls() -> None:
    """While a goal runs: status/pause/cancel are instant; ordinary messages
    and /open are rejected."""
    from harness import event_stream
    from harness.goal import runner as goal_runner

    class _FakeRunner:
        def is_alive(self):
            return True

        def is_running(self):
            return True

    tmp, ws = _tmp_workspace()
    with _active_workspace(ws):
        with mock.patch.object(goal_runner, "_runner", _FakeRunner()):
            assert goal_runner.is_goal_running()
            assert event_stream._is_goal_control_command("/goal status")
            assert event_stream._is_goal_control_command("/goal pause")
            assert event_stream._is_goal_control_command("/goal cancel")
            assert not event_stream._is_goal_control_command("/goal resume")
            assert not event_stream._is_goal_control_command("/goal --verify pytest -q -- x")

            emitted = []
            with mock.patch("harness.event_stream.emit", side_effect=lambda *a, **k: emitted.append((a, k))):
                context, interrupted, binding = event_stream._run_user_turn("hello", [], {}, None)
            assert not interrupted
            assert any("Goal is running" in str(k.get("text", "")) for _, k in emitted), emitted

            emitted.clear()
            with mock.patch("harness.event_stream.emit", side_effect=lambda *a, **k: emitted.append((a, k))):
                context, interrupted, binding = event_stream._run_user_turn("/open /some/dir", [], {}, None)
            assert not interrupted
            assert any("goal" in str(k.get("text", "")).lower() for _, k in emitted), emitted
    tmp.cleanup()


# --- G017: atomic store -------------------------------------------------------

def case_g017_atomic_store() -> None:
    """os.replace failure leaves the old goal.json intact, no tmp litter."""
    from harness.goal import store

    tmp, ws = _tmp_workspace()
    try:
        state = _new_state(ws)
        store.save_goal(state)
        path = store.goal_path(ws)
        original = path.read_text(encoding="utf-8")

        with mock.patch("harness.goal.store.os.replace", side_effect=OSError("disk full")):
            try:
                store.save_goal(state)
            except OSError:
                pass
            else:
                raise AssertionError("expected OSError from mocked replace")

        assert path.read_text(encoding="utf-8") == original
        leftovers = list(path.parent.glob("*.tmp"))
        assert not leftovers, f"temp files left behind: {leftovers}"
        assert store.load_goal(workspace=ws) is not None
    finally:
        tmp.cleanup()


# --- G018: corrupt state ------------------------------------------------------

def case_g018_corrupt_state() -> None:
    """Corrupt goal.json is reported, never overwritten, resume refused."""
    from harness.goal import store
    from harness.goal.commands import handle_goal_command

    tmp, ws = _tmp_workspace()
    try:
        state = _new_state(ws)
        store.save_goal(state)
        path = store.goal_path(ws)

        path.write_text("{broken", encoding="utf-8")
        try:
            store.load_goal(workspace=ws)
        except store.GoalStoreError as exc:
            assert exc.code == "goal_state_corrupt"
        else:
            raise AssertionError("corrupt goal.json must raise GoalStoreError")

        with _active_workspace(ws):
            note = handle_goal_command("/goal status", [], {}, None)
            assert "corrupt" in note.lower(), note
            assert path.read_text(encoding="utf-8") == "{broken"

            note = handle_goal_command("/goal resume", [], {}, None)
            assert "corrupt" in note.lower(), note
            assert path.read_text(encoding="utf-8") == "{broken"

        good = _new_state(ws)
        raw = json.loads(path.read_text(encoding="utf-8")) if False else {"schema_version": 999}
        path.write_text(json.dumps(raw), encoding="utf-8")
        try:
            store.load_goal(workspace=ws)
        except store.GoalStoreError as exc:
            assert exc.code == "unsupported_schema"
        else:
            raise AssertionError("unsupported schema must raise GoalStoreError")
    finally:
        tmp.cleanup()


# --- G019: workspace isolation ------------------------------------------------

def case_g019_workspace_isolation() -> None:
    """Goal state is per-workspace; switching workspaces reads the new one."""
    from harness.goal import store

    tmp = tempfile.TemporaryDirectory()
    try:
        ws_a = Path(tmp.name) / "a"
        ws_b = Path(tmp.name) / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        store.save_goal(_new_state(ws_a))
        assert store.load_goal(workspace=ws_a) is not None
        assert store.load_goal(workspace=ws_b) is None
        assert not (ws_b / ".project" / "goal.json").exists()

        with _active_workspace(ws_b):
            store.save_goal(_new_state(ws_b))
            loaded = store.load_goal()
            assert Path(loaded.workspace).resolve() == ws_b.resolve()
        assert store.load_goal(workspace=ws_a) is not None

        state_a = store.load_goal(workspace=ws_a)
        store.archive_goal(state_a, workspace=ws_a)
        assert (ws_a / ".project" / "goal-history" / f"{state_a.id}.json").exists()
    finally:
        tmp.cleanup()


# --- G020: non-interactive permission ask -------------------------------------

def case_g020_noninteractive_permission_ask() -> None:
    """An `ask` during goal ACT never blocks/reads stdin — it rejects the tool
    and pauses the goal with permission_wait."""
    from harness.goal import runner
    from harness.permissions.engine import PermissionDecision

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        asked = []

        def fake_eval(tool_name, tool_input=None, **kw):
            return PermissionDecision(
                effect="ask",
                tool=tool_name,
                resource=str((tool_input or {}).get("path") or ""),
                reason="ask for test",
            )

        def ask_triggering_agent(messages, context, **kw):
            from harness.hooks import trigger_hooks

            block = {"type": "tool_use", "name": "write_file", "input": {"path": str(ws / "out.py")}}
            result = trigger_hooks("PreToolUse", block)
            assert result is not None, "write_file ask must be rejected non-interactively"
            assert "non-interactive" in result
            messages.append({"role": "assistant", "content": "attempted"})
            kw["stats"].llm_rounds = 1
            return False

        with _MultiPatch([
            mock.patch.object(runner, "agent_loop", ask_triggering_agent),
            mock.patch.object(runner, "plan_features", _single_plan),
            mock.patch("harness.hooks.evaluate_permission", fake_eval),
            mock.patch(
                "harness.ui.permission_prompt.ask_permission",
                side_effect=lambda *a, **k: asked.append(1) or "n",
            ),
        ]):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()

        assert state.status == "paused"
        assert state.stop_reason == "permission_wait"
        assert not asked, "interactive ask must never be invoked during a goal ACT"
    tmp.cleanup()


# --- G021: model switch during goal -------------------------------------------

def case_g021_model_switch_during_goal() -> None:
    """/model stays instant while a goal runs; the next ACT reads the new model."""
    from harness import event_stream
    from harness.agent.recovery import RecoveryState
    from harness.goal import runner as goal_runner
    from harness.loop import call_llm
    import harness.loop as loop_mod

    class _FakeRunner:
        def is_alive(self):
            return True

        def is_running(self):
            return True

    tmp, ws = _tmp_workspace()
    with _active_workspace(ws):
        with mock.patch.object(goal_runner, "_runner", _FakeRunner()):
            assert goal_runner.is_goal_running()
            # The model switch command is instant (never the running UI).
            assert event_stream._is_instant_slash_command("/model deepseek-v4-pro")
            assert not event_stream._is_goal_control_command("/model deepseek-v4-pro")

        # call_llm resolves the model at call time (new model -> next ACT).
        captured = {}

        class _FakeResponse:
            stop_reason = "end_turn"
            content = "ok"

        with mock.patch.object(loop_mod, "get_model", return_value="goal-next-model"), \
             mock.patch.object(loop_mod, "with_retry", lambda fn, state: fn()), \
             mock.patch.object(loop_mod, "create_message", lambda **kw: captured.update(kw) or _FakeResponse()):
            call_llm([], {"system_override": "x"}, [], RecoveryState(), 100)
        assert captured["model_id"] == "goal-next-model"
    tmp.cleanup()


# --- G022: no agent self-completion -------------------------------------------

def case_g022_no_agent_self_completion() -> None:
    """The agent's 'I'm done' reply never completes the goal — verification is
    authoritative (machine result > agent claim)."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=3, max_consecutive_failures=3)

        def claiming_agent(messages, context, **kw):
            _write_code(ws, 1)
            messages.append({"role": "assistant", "content": "Done! All fixed."})
            kw["stats"].llm_rounds = 1
            return False

        _drive_goal(state, ws, agent=claiming_agent, verify=_mock_verify_fail)
        assert state.status == "failed", (
            f"agent self-report must not complete the goal: {state.status}"
        )
        assert state.phase != "done"
        from harness.features import get_feature

        assert get_feature(state.feature_id, workspace=ws).state != "passing"
    tmp.cleanup()


# --- G023: tasks dir follows the active workspace -----------------------------

def case_g023_tasks_dir_follows_workspace() -> None:
    """harness.tasks writes to the ACTIVE workspace, not the startup one —
    so a goal after /open never pollutes the old project's board."""
    import harness.tasks as tasks_mod

    tmp = tempfile.TemporaryDirectory()
    try:
        ws = Path(tmp.name) / "proj"
        ws.mkdir()
        before = tasks_mod._tasks_dir()

        with _active_workspace(ws):
            task = tasks_mod.create_task("new-ws task", "desc")
            assert Path(task.id and str(tasks_mod._tasks_dir() / f"{task.id}.json")).exists()

        assert not (before / f"{task.id}.json").exists()
        assert (ws / ".tasks" / f"{task.id}.json").exists()
    finally:
        tmp.cleanup()


# --- pollution guard ----------------------------------------------------------

def case_g_pollution_guard() -> None:
    """The G-series must never touch the real workspace's goal/task/feature data."""
    real_goal = _REAL_GOAL_FILE.read_bytes() if _REAL_GOAL_FILE.exists() else None
    real_archive = len(list(_REAL_TASKS_ARCHIVE.glob("task_*.json"))) if _REAL_TASKS_ARCHIVE.exists() else 0
    real_features = len(list(_REAL_FEATURES.glob("feat_*.json"))) if _REAL_FEATURES.exists() else 0
    assert (real_goal, real_archive, real_features) == _IMPORT_POLLUTION, (
        "G-series polluted the real workspace: "
        f"goal={_IMPORT_POLLUTION[0] != real_goal} "
        f"tasks_archive={_IMPORT_POLLUTION[1]}->{real_archive} "
        f"features={_IMPORT_POLLUTION[2]}->{real_features}"
    )


# --- G024–G028: L6 v2 decomposition --------------------------------------------

def _drive_multi(state, ws, *, verify=_mock_verify_pass, agent=None, full_verify=None, approve_plan=True):
    """Drive a runner with the 3-feature planner stub (dependency chain).

    ``approve_plan=False`` stops at the plan confirmation gate (PAUSED).
    """
    from harness.goal import runner

    calls = {"n": 0}
    recorded = []
    fake_agent = agent or _mock_agent_writes(ws, calls)

    def recording_agent(messages, context, **kw):
        prompt = ""
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                prompt = msg["content"]
        recorded.append(prompt)
        return fake_agent(messages, context, **kw)

    patches = [
        mock.patch.object(runner, "agent_loop", recording_agent),
        mock.patch.object(runner, "verify_feature_command", verify),
        mock.patch.object(runner, "plan_features", _multi_plan),
    ]
    if full_verify is not None:
        patches.append(mock.patch.object(runner, "run_verification", full_verify))
    with _MultiPatch(patches):
        r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
        r.run()
        if (
            approve_plan
            and state.status == "paused"
            and any(t["reason"] == "plan_ready" for t in state.transition_log)
        ):
            from harness.goal.engine import GoalEngine

            GoalEngine().transition(state, "select_feature", "resumed")
            runner.save_goal(state)
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()
    return calls, recorded


def _full_verify_pass(command, workspace=None, **kw):
    from harness.verification.runner import VerificationRunResult

    return VerificationRunResult(
        command=command, exit_code=0, stdout="ok", timed_out=False, duration_ms=1.0
    )


def _full_verify_fail(command, workspace=None, **kw):
    from harness.verification.runner import VerificationRunResult

    return VerificationRunResult(
        command=command, exit_code=1, stdout="boom", timed_out=False, duration_ms=1.0
    )


def case_g024_initialize_decomposes() -> None:
    """A decomposed goal creates one feature per plan item with dependency edges."""
    import harness.tasks as tasks_mod
    from harness.features import get_feature, list_features
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        with _MultiPatch([
            mock.patch.object(runner, "agent_loop", _mock_agent_writes(ws, {"n": 0})),
            mock.patch.object(runner, "verify_feature_command", _mock_verify_pass),
            mock.patch.object(runner, "plan_features", _multi_plan),
            mock.patch.object(runner, "run_verification", _full_verify_pass),
        ]):
            r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
            r.run()
            if state.status == "paused" and any(
                t["reason"] == "plan_ready" for t in state.transition_log
            ):
                from harness.goal.engine import GoalEngine

                GoalEngine().transition(state, "select_feature", "resumed")
                runner.save_goal(state)
                r = runner.GoalRunner(state=state, history=[], context={}, binding=None)
                r.run()

        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        assert len(state.feature_ids) == 3
        features = {f.name: f for f in list_features(workspace=ws)}
        assert set(features) == {"feat-a", "feat-b", "feat-c"}
        assert features["feat-a"].depends_on == []
        assert features["feat-b"].depends_on == [features["feat-a"].id]
        assert features["feat-c"].depends_on == [features["feat-b"].id]
        # Task links all features.
        task = tasks_mod.load_task(state.task_id)
        assert set(task.feature_ids) == set(state.feature_ids)
        assert task.status == "completed"
        # Whole-goal gate ran for a decomposed goal.
        assert any(t["reason"] == "full_verification_passed" for t in state.transition_log)
    tmp.cleanup()


def case_g025_dependency_order() -> None:
    """Features execute in dependency order (feat-a -> feat-b -> feat-c)."""
    import re

    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        calls, recorded = _drive_multi(state, ws, full_verify=_full_verify_pass)
        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        assert len(state.feature_ids) == 3

        # Extract the feature *name* each ACT prompt was scoped to.
        order = []
        for prompt in recorded:
            match = re.search(r"Feature: \w+ \((.+)\)", prompt)
            if match:
                order.append(match.group(1))
        assert order == ["feat-a", "feat-b", "feat-c"], f"wrong execution order: {order}"

        # Every feature ended passing; the whole-goal gate ran once.
        from harness.features import get_feature

        for fid in state.feature_ids:
            assert get_feature(fid, workspace=ws).state == "passing"
        assert any(t["reason"] == "full_verification_passed" for t in state.transition_log)
    tmp.cleanup()


def case_g026_plan_parse_fault_tolerant() -> None:
    """Malformed plans degrade to a single feature; policy-rejected
    verification commands fall back to the full command."""
    from harness.goal.planner import FeaturePlan, parse_plan, plan_features

    # Valid chain parses.
    plans = parse_plan(
        '[{"name": "a", "behavior": "x", "verification": "pytest tests/a.py -q", "depends_on": []},'
        '{"name": "b", "behavior": "y", "verification": "", "depends_on": ["a"]}]'
    )
    assert plans is not None and len(plans) == 2
    assert plans[0].verification == "pytest tests/a.py -q"
    assert plans[1].depends_on == ("a",)

    # run_agent_task prefixes the output with a header line — must be stripped
    # before JSON extraction (regression: the header's '[' broke parsing and
    # silently degraded real decompositions to a single feature).
    plans = parse_plan(
        "[explore / mock] decompose goal into verifiable features (8 tools, 18.6s)\n\n"
        '[{"name": "a", "behavior": "x", "verification": "", "depends_on": []},'
        '{"name": "b", "behavior": "y", "verification": "", "depends_on": ["a"]}]'
    )
    assert plans is not None and len(plans) == 2, f"header must be stripped: {plans}"

    # Policy-rejected verification falls back to "" (full verify covers it).
    plans = parse_plan('[{"name": "a", "behavior": "x", "verification": "rm -rf /", "depends_on": []}]')
    assert plans is not None and plans[0].verification == ""

    # Garbage / empty / forward deps / missing fields -> None.
    assert parse_plan("no json here") is None
    assert parse_plan("[]") is None
    assert parse_plan('[{"name": "a", "behavior": "x", "depends_on": ["later"]}]') is None
    assert parse_plan('[{"name": "a", "behavior": "x", "verification": "rm -rf /", "depends_on": []},'
                      '{"name": "b", "behavior": "", "depends_on": []}]') is None

    # planner falls back to a single feature when the agent output is garbage.
    plans = plan_features("target", "pytest -q", ws := Path(tempfile.mkdtemp()),
                          planner_runner=lambda *a, **k: "[explore] nothing here")
    assert len(plans) == 1
    assert plans[0].name == "target"
    assert plans[0].verification == ""


def case_g027_full_verification_gate() -> None:
    """Whole-goal --verify gate failure -> FAILED/full_verification_failed."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        _drive_multi(state, ws, full_verify=_full_verify_fail)
        assert state.status == "failed"
        assert state.stop_reason == "full_verification_failed"
    tmp.cleanup()


def case_g028_feature_fuse_stops_goal() -> None:
    """A feature that exhausts its retry budget fails the goal (feature_failed)
    and later features never run."""
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws, max_attempts=10, max_consecutive_failures=2)
        # First feature passes, second keeps failing -> fuse on feat-b.
        fail_for = {"feat-b"}

        def feature_aware_verify(feature_id, workspace=None, **kw):
            from harness.features import get_feature, verify_feature

            feature = get_feature(feature_id, workspace)
            if feature.name in fail_for:
                return verify_feature(
                    feature_id, False, error="verification failed with exit code 1",
                    workspace=workspace,
                )
            from harness.features.schema import VerificationEvidence

            return verify_feature(
                feature_id, True,
                VerificationEvidence(command="python check.py", exit_code=0, verified_by="harness"),
                workspace=workspace,
            )

        calls, recorded = _drive_multi(state, ws, verify=feature_aware_verify)
        assert state.status == "failed"
        assert state.stop_reason == "feature_failed"
        assert "feat-b" in str(state.last_error)
        # feat-a worked, feat-b burned its budget, feat-c never ran.
        prompts = "\n".join(recorded)
        assert "feat-a" in prompts and "feat-b" in prompts
        assert "feat-c" not in prompts
    tmp.cleanup()


def case_g029_plan_confirmation_gate() -> None:
    """A decomposed goal pauses at the plan gate until approved; nothing runs
    before approval; resume proceeds with the plan."""
    from harness.features import get_feature
    from harness.goal import runner

    tmp, ws = _tmp_workspace()
    _git_init(ws)
    with _active_workspace(ws):
        state = _new_state(ws)
        calls, recorded = _drive_multi(state, ws, approve_plan=False)
        assert state.status == "paused", (state.status, state.stop_reason)
        assert any(t["reason"] == "plan_ready" for t in state.transition_log)
        assert calls["n"] == 0, "nothing may run before plan approval"
        assert recorded == []
        for fid in state.feature_ids:
            assert get_feature(fid, workspace=ws).state == "not_started"

        # Plan is visible in status output.
        status = runner.get_goal_status()
        assert "Plan ready" in status
        assert f"{len(state.feature_ids)}" in status.split("Features:")[1].split("\n")[0]

        # Approving (resume) runs the plan to completion.
        calls, recorded = _drive_multi(state, ws, approve_plan=True, full_verify=_full_verify_pass)
        assert state.status == "done", (state.status, state.stop_reason, state.last_error)
        assert any(t["reason"] == "resumed" for t in state.transition_log)
    tmp.cleanup()


CASES = [
    EvalCase("g001.legal_transitions", "G001: legal goal transitions pass; illegal raise GoalTransitionError", "goal", case_g001_legal_transitions),
    EvalCase("g002.initialize_creates_graph", "G002: initialize creates exactly 1 task + 1 feature (WIP=1, restricted tools)", "goal", case_g002_initialize_creates_graph),
    EvalCase("g003.verification_pass_done", "G003: verification pass + clean ok -> DONE, task completed", "goal", case_g003_verification_pass_done),
    EvalCase("g004.verification_fail_retries", "G004: verification fail retries with attempts accumulating", "goal", case_g004_verification_fail_retries),
    EvalCase("g005.max_failures_fuse", "G005: max consecutive failures fuse stops the goal", "goal", case_g005_max_failures_fuse),
    EvalCase("g006.max_duration_fuse", "G006: max duration fuse fails the goal", "goal", case_g006_max_duration_fuse),
    EvalCase("g007.no_progress_fuse", "G007: no-progress fuse stops the goal", "goal", case_g007_no_progress_fuse),
    EvalCase("g008.pause_resume", "G008: pause mid-ACT -> PAUSED (durable); resume -> SELECT_FEATURE", "goal", case_g008_pause_resume),
    EvalCase("g009.cancel", "G009: cancel -> CANCELLED terminal, history archived", "goal", case_g009_cancel),
    EvalCase("g010.process_restart_recovery", "G010: running goal on disk loads as paused/process_restarted", "goal", case_g010_process_restart_recovery),
    EvalCase("g011.wip_one", "G011: WIP=1 machine constraint — only the goal's own feature is claimed", "goal", case_g011_wip_one),
    EvalCase("g012.stale_passing", "G012: stale passing feature -> ACT (reopen), never DONE directly", "goal", case_g012_stale_passing),
    EvalCase("g013.clean_failure", "G013: clean failure -> ACT retry; cap -> FAILED/clean_check_failed", "goal", case_g013_clean_failure),
    EvalCase("g014.evaluator_advisory", "G014: evaluator runs once, advisory only", "goal", case_g014_evaluator_advisory),
    EvalCase("g015.command_parsing", "G015: /goal command parsing and validation", "goal", case_g015_command_parsing),
    EvalCase("g016.tui_busy_controls", "G016: goal running blocks ordinary turns, allows status/pause/cancel", "goal", case_g016_tui_busy_controls),
    EvalCase("g017.atomic_store", "G017: goal writes are atomic (os.replace failure keeps old file)", "goal", case_g017_atomic_store),
    EvalCase("g018.corrupt_state", "G018: corrupt goal state is reported and never overwritten", "goal", case_g018_corrupt_state),
    EvalCase("g019.workspace_isolation", "G019: goal state is isolated per workspace", "goal", case_g019_workspace_isolation),
    EvalCase("g020.noninteractive_permission_ask", "G020: non-interactive permission ask pauses the goal (permission_wait)", "goal", case_g020_noninteractive_permission_ask),
    EvalCase("g021.model_switch_during_goal", "G021: model switch is instant and does not cancel the goal", "goal", case_g021_model_switch_during_goal),
    EvalCase("g022.no_agent_self_completion", "G022: agent self-reported done never completes the goal", "goal", case_g022_no_agent_self_completion),
    EvalCase("g023.tasks_dir_follows_workspace", "G023: tasks dir follows the active workspace", "goal", case_g023_tasks_dir_follows_workspace),
    EvalCase("g024.initialize_decomposes", "G024: /goal decomposes into a dependency-ordered feature plan", "goal", case_g024_initialize_decomposes),
    EvalCase("g025.dependency_order", "G025: features execute in dependency order, whole-goal gate runs", "goal", case_g025_dependency_order),
    EvalCase("g026.plan_parse_fault_tolerant", "G026: malformed plans degrade to a single feature; policy-filtered verify", "goal", case_g026_plan_parse_fault_tolerant),
    EvalCase("g027.full_verification_gate", "G027: whole-goal --verify gate failure -> FAILED/full_verification_failed", "goal", case_g027_full_verification_gate),
    EvalCase("g028.feature_fuse_stops_goal", "G028: a feature that exhausts its budget fails the goal; later features never run", "goal", case_g028_feature_fuse_stops_goal),
    EvalCase("g029.plan_confirmation_gate", "G029: decomposed plan pauses at the confirmation gate until /goal resume", "goal", case_g029_plan_confirmation_gate),
    EvalCase("g999.pollution_guard", "G-pollution: real workspace goal/task/feature data untouched", "goal", case_g_pollution_guard),
]
