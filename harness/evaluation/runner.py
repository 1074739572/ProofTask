"""Independent evaluator runner (L5, advisory).

Runs the read-only ``evaluator`` subagent (mimo-v2.5-pro per agents.json) on
a feature's collected inputs, parses the JSON findings, and records them on
the feature via ``harness.features.record_evaluation``.

Design rules (reliability plan §4 L5):

- the evaluator NEVER changes feature state — machine verification (L3)
  remains the only gate into ``passing``;
- parse failures are recorded as ``passed: null`` + error, never raise;
- only runs when the model profile is available (``validate_agent_model``);
- simple tasks can skip evaluation (caller decides — see
  ``requires_evaluation`` heuristic).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from harness.agents.registry import get_agent_profile, validate_agent_model
from harness.agents.runner import run_agent_task
from harness.evaluation.inputs import collect_inputs
from harness.evaluation.parser import Findings, parse_findings
from harness.features import Feature, get_feature, record_evaluation

EVALUATOR_AGENT = "evaluator"


def requires_evaluation(feature: Feature) -> bool:
    """Explicit opt-in — simple tasks skip the evaluator to save tokens.

    Set ``evaluation_required=True`` when creating the feature (no implicit
    heuristic: a pytest-verified feature can still need an evaluator when the
    requirement is about behavior/scope, not just test pass/fail).
    """
    return bool(feature.evaluation_required)


def run_evaluation(
    feature_id: str,
    workspace: str | Path | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
) -> Feature:
    """Run the independent evaluator and record findings on the feature.

    Returns the updated Feature. Never raises for model/parse problems —
    failures are recorded on ``feature.evaluation.error``.
    """
    feature: Feature = get_feature(feature_id, workspace)
    inputs = collect_inputs(feature_id, workspace)
    evaluated_at = time.time()

    error = validate_agent_model(EVALUATOR_AGENT)
    if error is not None:
        return record_evaluation(
            feature_id,
            {
                "passed": None,
                "summary": "",
                "findings": [],
                "evaluated_by": EVALUATOR_AGENT,
                "evaluated_at": evaluated_at,
                "error": f"evaluator agent unavailable: {error}",
            },
            workspace=workspace,
        )

    profile = get_agent_profile(EVALUATOR_AGENT)
    prompt = (
        "Evaluate the following feature implementation. "
        f"Return ONLY a JSON object with keys passed/summary/findings.\n\n"
        f"{inputs.to_text()}"
    )
    raw = run_agent_task(
        description=f"evaluate feature {feature_id} ({feature.name})",
        prompt=prompt,
        agent_type=EVALUATOR_AGENT,
        cwd=str(feature.workspace or workspace) if (feature.workspace or workspace) else None,
        cancel_check=cancel_check,
        deadline=deadline,
        stats=stats,
    )
    parsed: Findings = parse_findings(raw)

    payload: dict[str, Any] = parsed.to_dict()
    payload.update(
        {
            "evaluated_by": profile.model_id if profile else EVALUATOR_AGENT,
            "evaluated_at": evaluated_at,
        }
    )
    return record_evaluation(feature_id, payload, workspace=workspace)
