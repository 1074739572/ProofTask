"""Goal-mode evals for the strict Goal -> Task contract."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from evals.types import EvalCase


def _task_board(tmp: Path):
    import harness.tasks as tasks
    return mock.patch.object(tasks, "TASKS_DIR", tmp / ".tasks")


def case_g001_task_phases() -> None:
    from harness.goal.engine import GoalEngine, GoalTransitionError
    from harness.goal.models import GoalPhase, GoalState

    state = GoalState.new(target="x", verification="pytest -q", workspace=".")
    engine = GoalEngine()
    engine.transition(state, GoalPhase.SELECT_TASK, "init")
    engine.transition(state, GoalPhase.CLAIM, "select")
    try:
        engine.transition(state, GoalPhase.DONE, "illegal")
        assert False, "Task cannot skip verification"
    except GoalTransitionError:
        pass


def case_g002_plan_uses_catalog_only() -> None:
    from harness.goal.planner import parse_plan
    from harness.verification.catalog import TestCatalog

    catalog = TestCatalog(selectors=("tests/test_x.py::test_x",), test_files=("tests/test_x.py",))
    plans = parse_plan('[{"name":"x","behavior":"b","acceptance_cases":[{"id":"AC1","given":"i","when":"w","then":"t"}],"test_selectors":["tests/test_x.py::test_x"]}]', test_catalog=catalog)
    assert plans and plans[0].verification_spec.source == "discovered"
    unknown = parse_plan('[{"name":"x","behavior":"b","acceptance_cases":[{"id":"AC1","given":"i","when":"w","then":"t"}],"test_selectors":["tests/no.py::test_no"]}]', test_catalog=catalog)
    assert unknown and unknown[0].verification_spec.source == "needs_generation"


def case_g003_task_evidence_gates_completion() -> None:
    import harness.tasks as tasks

    with tempfile.TemporaryDirectory() as root, _task_board(Path(root)):
        task = tasks.create_task("x", "b", verification_spec={"source": "generated", "command": "pytest -q tests/test_x.py::test_x", "selectors": ["tests/test_x.py::test_x"], "collected_count": 1})
        tasks.claim_task(task.id)
        assert "has not passed" in tasks.complete_task(task.id)
        tasks.set_task_verification_result(task.id, passed=True, evidence={"command": "pytest -q tests/test_x.py::test_x", "exit_code": 0})
        with mock.patch.dict("os.environ", {"HARNESS_CLEAN_MODE": "off"}):
            assert tasks.complete_task(task.id).startswith("Completed")


def case_g004_dependencies_are_task_edges() -> None:
    import harness.tasks as tasks

    with tempfile.TemporaryDirectory() as root, _task_board(Path(root)):
        first = tasks.create_task("first", "b")
        second = tasks.create_task("second", "b", [first.id])
        assert not tasks.can_start(second.id)
        tasks.claim_task(first.id)
        with mock.patch.dict("os.environ", {"HARNESS_CLEAN_MODE": "off"}):
            tasks.complete_task(first.id)
        assert tasks.can_start(second.id)


def case_g005_old_goal_schema_rejected() -> None:
    from harness.goal.store import GoalStoreError, goal_path, load_goal

    with tempfile.TemporaryDirectory() as root:
        path = goal_path(root)
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "feature_id": "feat_old"}', encoding="utf-8")
        try:
            load_goal(root)
            assert False, "old mixed Feature schema must not be migrated"
        except GoalStoreError as exc:
            assert exc.code == "unsupported_schema"


CASES = [
    EvalCase("g001.task_phases", "G001: Task phases cannot skip verification", "goal", case_g001_task_phases),
    EvalCase("g002.catalog_binding", "G002: planner binds only collected tests", "goal", case_g002_plan_uses_catalog_only),
    EvalCase("g003.evidence_gate", "G003: passing evidence gates Task completion", "goal", case_g003_task_evidence_gates_completion),
    EvalCase("g004.task_dependencies", "G004: Task dependencies control scheduling", "goal", case_g004_dependencies_are_task_edges),
    EvalCase("g005.strict_schema", "G005: old Goal Feature schema is rejected", "goal", case_g005_old_goal_schema_rejected),
]
