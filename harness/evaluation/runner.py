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
from harness.evaluation.inputs import collect_inputs, collect_task_inputs
from harness.evaluation.parser import Findings, parse_findings
from harness.features import Feature, get_feature, record_evaluation

EVALUATOR_AGENT = "evaluator"


def build_evaluation_prompt(inputs) -> str:
    """Prompt shared by every evaluator run.

    The evaluator has useful judgment on coverage and scope, but deterministic
    verification facts are not up for interpretation by the model.
    """
    return (
        "You are an independent evaluator for one Task. Return ONLY a JSON object "
        "with keys passed, route, summary, findings, and affected_task_ids.\n\n"
        "Hard rules:\n"
        "- Judge every acceptance case against the diff and evidence.\n"
        "- A missing binding, zero collected tests, a non-zero verification result, "
        "or a command/evidence mismatch is never passable. Report it as a high finding.\n"
        "- Passing tests are necessary but not sufficient: report unmet behavior, "
        "weak tests, and unrelated scope changes.\n"
        "- route must be pass, implementation_fix, test_gap, replan, or blocked.\n"
        "- You are advisory. Do not claim to change Task state.\n\n"
        f"{inputs.to_text()}"
    )


def _contract_evidence_error(feature: Feature) -> str | None:
    """Return deterministic evidence errors for a structured Task contract."""
    spec = feature.verification_spec or {}
    if not isinstance(spec, dict) or not spec:
        return None
    source = str(spec.get("source") or "")
    try:
        collected_count = int(spec.get("collected_count") or 0)
    except (TypeError, ValueError):
        collected_count = 0
    if source in {"discovered", "generated"} and collected_count <= 0:
        return "bound verification collected zero tests"
    if source in {"discovered", "generated"} and not feature.evidence:
        return "bound verification has no machine evidence"
    command = str(spec.get("command") or "")
    if command and feature.evidence:
        last_evidence = feature.evidence[-1]
        evidence_command = str(last_evidence.get("command") or "")
        if evidence_command and evidence_command != command:
            return "verification evidence command does not match the bound command"
        try:
            exit_code = int(last_evidence.get("exit_code") or 0)
        except (TypeError, ValueError):
            exit_code = -1
        if exit_code != 0:
            return "bound verification evidence has a non-zero exit code"
    return None


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
    prompt = build_evaluation_prompt(inputs)
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
    evidence_error = _contract_evidence_error(feature)
    if evidence_error:
        payload["passed"] = False
        payload["summary"] = evidence_error
        payload.setdefault("findings", []).append(
            {"issue": evidence_error, "severity": "high", "evidence": "VerificationSpec/evidence"}
        )
    payload.update(
        {
            "evaluated_by": profile.model_id if profile else EVALUATOR_AGENT,
            "evaluated_at": evaluated_at,
        }
    )
    return record_evaluation(feature_id, payload, workspace=workspace)


def run_task_evaluation(
    task_id: str,
    workspace: str | Path,
    *,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
):
    """Run the advisory evaluator against a Task's own contract."""
    from harness.tasks import load_task, record_task_evaluation

    task = load_task(task_id)
    inputs = collect_task_inputs(task_id, workspace)
    evaluated_at = time.time()
    error = validate_agent_model(EVALUATOR_AGENT)
    if error is not None:
        return record_task_evaluation(task_id, {"passed": None, "summary": "", "findings": [], "evaluated_by": EVALUATOR_AGENT, "evaluated_at": evaluated_at, "error": f"evaluator agent unavailable: {error}"})
    profile = get_agent_profile(EVALUATOR_AGENT)
    raw = run_agent_task(
        description=f"evaluate task {task_id} ({task.subject})",
        prompt=build_evaluation_prompt(inputs),
        agent_type=EVALUATOR_AGENT,
        cwd=str(workspace),
        cancel_check=cancel_check,
        deadline=deadline,
        stats=stats,
    )
    payload = parse_findings(raw).to_dict()
    evidence_error = _contract_evidence_error(task)
    if evidence_error:
        payload["passed"] = False
        payload["summary"] = evidence_error
        payload.setdefault("findings", []).append({"issue": evidence_error, "severity": "high", "evidence": "VerificationSpec/evidence"})
    payload.update({"evaluated_by": profile.model_id if profile else EVALUATOR_AGENT, "evaluated_at": evaluated_at})
    return record_task_evaluation(task_id, payload)
