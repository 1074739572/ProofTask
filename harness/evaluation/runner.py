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

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from harness.agents.registry import get_agent_profile, validate_agent_model
from harness.agents.runner import run_agent_task
from harness.evaluation.inputs import collect_inputs, collect_task_inputs
from harness.evaluation.parser import Findings, parse_findings
from harness.features import Feature, get_feature, record_evaluation
from harness.verification.snapshot import capture_code_snapshot

EVALUATOR_AGENT = "evaluator"
MAX_RAW_OUTPUT_TAIL = 2_000


def _relative_scope_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _requires_scope_replan(task: Any, payload: dict[str, Any]) -> bool:
    """Return whether explicit required paths exceed the frozen Task scope."""
    approved = {
        _relative_scope_path(path)
        for path in (
            *getattr(task, "primary_write", []),
            *getattr(task, "planned_new", []),
            *getattr(task, "conditional_write", []),
        )
        if str(path).strip()
    }
    if not approved:
        return False
    requested = {
        _relative_scope_path(path)
        for path in (payload.get("required_write_paths") or [])
        if str(path).strip()
    }
    return any(
        not path.startswith("tests/")
        and not any(path == scope or path.startswith(f"{scope}/") for scope in approved)
        for path in requested
    )


def _run_evaluator(
    *,
    description: str,
    prompt: str,
    cwd: str | None,
    cancel_check: Callable[[], bool] | None,
    deadline: float | None,
    stats,
) -> tuple[Findings, dict[str, Any]]:
    """Run once, then make one JSON-only correction attempt when needed."""
    raw = run_agent_task(
        description=description,
        prompt=prompt,
        agent_type=EVALUATOR_AGENT,
        cwd=cwd,
        cancel_check=cancel_check,
        deadline=deadline,
        stats=stats,
    )
    parsed = parse_findings(raw)
    attempts = 1
    # Provider/cancel/deadline outcomes are not format errors. Retrying them
    # immediately only hides the real failure and burns another model call.
    stop_reason = str(getattr(stats, "stop_reason", "") or "")
    if parsed.passed is None and stop_reason not in {"provider_error", "configuration_error", "cancelled", "deadline"}:
        attempts = 2
        raw = run_agent_task(
            description=description + " (JSON correction)",
            prompt=(
                prompt
                + "\n\nYour previous response was not a valid verdict. Reply now with ONLY one valid JSON object, no prose or code fence."
            ),
            agent_type=EVALUATOR_AGENT,
            cwd=cwd,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
        parsed = parse_findings(raw)
        stop_reason = str(getattr(stats, "stop_reason", "") or "")
    payload = {
        "raw_output_tail": str(raw or "")[-MAX_RAW_OUTPUT_TAIL:],
        "raw_output_sha256": hashlib.sha256(str(raw or "").encode("utf-8", errors="replace")).hexdigest(),
        "parse_attempts": attempts,
        "agent_stop_reason": stop_reason or "completed",
    }
    if parsed.passed is None and stop_reason in {"provider_error", "configuration_error"}:
        parsed = Findings(passed=None, error="evaluator provider unavailable")
    return parsed, payload


def build_evaluation_prompt(inputs) -> str:
    """Prompt shared by every evaluator run.

    The evaluator has useful judgment on coverage and scope, but deterministic
    verification facts are not up for interpretation by the model.
    """
    from harness.goal.skills import assigned_skill_context

    review_skill = assigned_skill_context(("code-review",))
    skill_section = (
        "\n\nReview workflow guidance follows. It is advisory: the required JSON result and hard rules win.\n"
        f"{review_skill}\n"
        if review_skill
        else ""
    )
    return (
        "You are an independent evaluator for one Task. Return ONLY a JSON object "
        "with keys passed, route, summary, findings, affected_task_ids, and required_write_paths.\n\n"
        "Hard rules:\n"
        "- Judge every acceptance case against the diff and evidence.\n"
        "- A missing binding, zero collected tests, a non-zero verification result, "
        "or a command/evidence mismatch is never passable. Report it as a high finding.\n"
        "- For a Task, judge verification from the current evidence section. Earlier failed runs are audit history; "
        "a later matching successful bound verification supersedes them for the current verdict.\n"
        "- Route replan ONLY when an acceptance case requires changing a concrete production path outside the Task's "
        "approved writable scope. For that route, required_write_paths must be a non-empty JSON array containing only "
        "those concrete repository-relative paths. A path merely mentioned in evidence, history, or a stack trace is not "
        "a required write path. Do not route implementation_fix for work this Task is not authorized to perform.\n"
        "- Passing tests are necessary but not sufficient: report unmet behavior, "
        "weak tests, and unrelated scope changes.\n"
        "- When Cross-Task impact context is present, verify the bound tests and diff address it; "
        "missing required interaction coverage is never passable.\n"
        "- route must be pass, implementation_fix, test_gap, replan, or blocked.\n"
        "- You are advisory. Do not claim to change Task state."
        f"{skill_section}\n"
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
    parsed, diagnostics = _run_evaluator(
        description=f"evaluate feature {feature_id} ({feature.name})",
        prompt=prompt,
        cwd=str(feature.workspace or workspace) if (feature.workspace or workspace) else None,
        cancel_check=cancel_check,
        deadline=deadline,
        stats=stats,
    )

    payload: dict[str, Any] = parsed.to_dict()
    payload.update(diagnostics)
    payload["input_snapshot"] = capture_code_snapshot(feature.workspace or workspace)
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
    parsed, diagnostics = _run_evaluator(
        description=f"evaluate task {task_id} ({task.subject})",
        prompt=build_evaluation_prompt(inputs),
        cwd=str(workspace),
        cancel_check=cancel_check,
        deadline=deadline,
        stats=stats,
    )
    payload = parsed.to_dict()
    payload.update(diagnostics)
    payload["input_snapshot"] = capture_code_snapshot(workspace)
    evidence_error = _contract_evidence_error(task)
    if evidence_error:
        payload["passed"] = False
        payload["summary"] = evidence_error
        payload.setdefault("findings", []).append({"issue": evidence_error, "severity": "high", "evidence": "VerificationSpec/evidence"})
    if _requires_scope_replan(task, payload):
        payload["passed"] = False
        payload["route"] = "replan"
        payload["summary"] = "Evaluation requires source changes outside this Task's approved writable scope; replanning is required."
        payload.setdefault("findings", []).append({
            "issue": "The evaluator identified an implementation boundary outside this Task's approved writable scope.",
            "severity": "high",
            "evidence": "Task scope contract",
        })
    elif payload.get("route") == "replan":
        # A free-form evaluator explanation can mention related source files
        # without requiring a change there.  Keep the Task repairable unless
        # it supplied explicit unapproved write paths.
        payload["route"] = "implementation_fix"
        payload.setdefault("findings", []).append({
            "issue": "Replan request lacked an explicit unapproved required_write_paths entry.",
            "severity": "medium",
            "evidence": "Task scope contract",
        })
    payload.update({"evaluated_by": profile.model_id if profile else EVALUATOR_AGENT, "evaluated_at": evaluated_at})
    return record_task_evaluation(task_id, payload)
