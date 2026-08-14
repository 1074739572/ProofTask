"""Independent evaluator (L5) — advisory second opinion on features.

Layers:

- ``inputs.py``  — collect behavior / verification / evidence / diff / rubric
- ``parser.py``  — extract + validate the evaluator's JSON findings (pure)
- ``runner.py``  — run the read-only ``evaluator`` subagent, record findings

Public API::

    run_evaluation(feature_id, *, workspace) -> Feature
    requires_evaluation(feature) -> bool
    collect_inputs(feature_id, *, workspace) -> EvaluationInputs
    parse_findings(raw) -> Findings
"""

from harness.evaluation.inputs import (
    RUBRIC,
    EvaluationInputs,
    collect_inputs,
)
from harness.evaluation.parser import Findings, parse_findings
from harness.evaluation.runner import (
    EVALUATOR_AGENT,
    build_evaluation_prompt,
    requires_evaluation,
    run_evaluation,
    run_task_evaluation,
)

__all__ = [
    "RUBRIC",
    "EVALUATOR_AGENT",
    "build_evaluation_prompt",
    "EvaluationInputs",
    "Findings",
    "collect_inputs",
    "parse_findings",
    "requires_evaluation",
    "run_evaluation",
    "run_task_evaluation",
]
