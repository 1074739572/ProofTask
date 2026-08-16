"""Regression coverage for evaluator diagnostics and input prioritization."""

from __future__ import annotations

from types import SimpleNamespace

from harness.evaluation.inputs import EvaluationInputs
from harness.evaluation.parser import parse_findings
from harness.evaluation.runner import build_evaluation_prompt
from harness.goal.models import GoalState
from harness.goal.prompt import build_goal_act_prompt


def test_evaluator_rejects_contradictory_passing_verdicts():
    parsed = parse_findings(
        '{"passed":true,"route":"blocked","summary":"ok","findings":[]}'
    )
    assert parsed.passed is None
    assert "inconsistent" in str(parsed.error)


def test_evaluator_includes_bound_test_source_before_large_diff():
    feature = SimpleNamespace(
        behavior="behavior",
        acceptance_cases=[],
        verification_spec={},
        verification="python -m pytest -q",
        evidence=[],
        start_snapshot=None,
        start_diff=None,
    )
    rendered = EvaluationInputs(
        feature=feature,
        diff="x" * 30_000,
        bound_test_sources=[("tests/test_bound.py", "def test_bound(): pass")],
    ).to_text()
    assert "tests/test_bound.py" in rendered
    assert "def test_bound(): pass" in rendered


def test_cross_task_impact_context_is_explicit_in_worker_and_evaluator_prompts(tmp_path):
    task = SimpleNamespace(
        id="task_target",
        subject="approval flow",
        description="keep remembered approvals working",
        acceptance_cases=[],
        verification_state="needs_generation",
        verification_spec={
            "impact_context": [{
                "source_task_id": "task_upstream",
                "reason": "the shared permission gate changed",
                "required_coverage": "cover approval persistence through the gate",
            }],
        },
        evidence=[],
        last_error=None,
        repair_history=[],
        behavior="keep remembered approvals working",
        verification="pytest -q",
        start_snapshot=None,
        start_diff=None,
    )
    state = GoalState.new(target="preserve approvals", verification="pytest -q", workspace=str(tmp_path))

    worker_prompt = build_goal_act_prompt(state, task)
    evaluator_prompt = build_evaluation_prompt(EvaluationInputs(feature=task, diff=""))

    assert "Cross-Task impact context" in worker_prompt
    assert "Do not regress the upstream behavior" in worker_prompt
    assert "Cross-Task impact context" in evaluator_prompt
    assert "missing required interaction coverage is never passable" in evaluator_prompt
