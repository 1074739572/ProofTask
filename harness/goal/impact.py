"""Cross-Task test impact review after each completed Goal Task."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from harness.agents.runner import run_agent_task
from harness.goal.memory import load_test_map
from harness.goal.repair import _extract_json

IMPACT_AGENT = "goal_test_impact"


@dataclass(frozen=True)
class ImpactDecision:
    action: str = "none"  # none | add_tests
    task_id: str | None = None
    reason: str = ""
    unavailable: bool = False
    format_error: bool = False
    parse_attempts: int = 0


def _parse_impact_decision(raw: str, pending_tasks: list) -> ImpactDecision:
    block = _extract_json(raw)
    if block is None:
        return ImpactDecision(reason="impact reviewer returned no JSON", format_error=True)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return ImpactDecision(reason=f"impact reviewer returned invalid JSON: {exc.msg}", format_error=True)
    if not isinstance(data, dict):
        return ImpactDecision(reason="impact reviewer output is not an object", format_error=True)
    action = str(data.get("action") or "")
    task_id = str(data.get("task_id") or "")
    valid_ids = {task.id for task in pending_tasks}
    if action == "none":
        return ImpactDecision()
    if action == "add_tests" and task_id in valid_ids:
        return ImpactDecision("add_tests", task_id, str(data.get("reason") or "")[:1200])
    return ImpactDecision(
        reason="impact reviewer returned an unsupported action or pending task id",
        format_error=True,
    )


def _agent_stop_reason(stats) -> str:
    return str(getattr(stats, "stop_reason", "") or "completed")


def review_test_impact(
    state,
    completed_task,
    pending_tasks: list,
    *,
    cwd: str,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
    runner=None,
) -> ImpactDecision:
    if not pending_tasks:
        return ImpactDecision()
    candidates = [
        {
            "id": task.id,
            "subject": task.subject,
            "behavior": task.description,
            "depends_on": task.blockedBy,
            "acceptance_cases": task.acceptance_cases,
        }
        for task in pending_tasks
    ]
    prompt = (
        "Review whether a completed Task requires additional cross-Task coverage before a pending Task starts. "
        "Return ONLY JSON: {\"action\":\"none|add_tests\",\"task_id\":\"pending task id or null\",\"reason\":\"...\"}.\n"
        "Only choose add_tests for a dependency, shared module, or public interface interaction that the existing TestMap does not cover. "
        "Do not invent product scope or ask the user.\n\n"
        f"Goal contract: {json.dumps(state.goal_contract, ensure_ascii=False)}\n"
        f"Completed task: {json.dumps({'id': completed_task.id, 'subject': completed_task.subject, 'behavior': completed_task.description, 'acceptance_cases': completed_task.acceptance_cases}, ensure_ascii=False)}\n"
        f"Pending tasks: {json.dumps(candidates, ensure_ascii=False)}\n"
        f"TestMap: {json.dumps(load_test_map(state)[-24:], ensure_ascii=False)}"
    )
    invoke = runner or run_agent_task
    try:
        raw = invoke(
            description=f"test impact after task {completed_task.id}",
            prompt=prompt,
            agent_type=IMPACT_AGENT,
            cwd=cwd,
            max_rounds=16,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception as exc:
        return ImpactDecision(reason=f"impact reviewer unavailable: {type(exc).__name__}", unavailable=True)
    if _agent_stop_reason(stats) in {"provider_error", "configuration_error"}:
        return ImpactDecision(reason="impact reviewer provider unavailable", unavailable=True)

    decision = _parse_impact_decision(raw, pending_tasks)
    if not decision.format_error:
        return decision

    # Prompt compliance is probabilistic even when the provider completed the
    # request. Make one bounded correction attempt before requiring attention.
    try:
        corrected = invoke(
            description=f"test impact after task {completed_task.id} (JSON correction)",
            prompt=(
                prompt
                + "\n\nYour previous response was not a valid impact decision. "
                "Reply now with ONLY one valid JSON object matching the requested schema, "
                "with no prose or code fence."
            ),
            agent_type=IMPACT_AGENT,
            cwd=cwd,
            max_rounds=16,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception as exc:
        return ImpactDecision(reason=f"impact reviewer unavailable: {type(exc).__name__}", unavailable=True)
    if _agent_stop_reason(stats) in {"provider_error", "configuration_error"}:
        return ImpactDecision(reason="impact reviewer provider unavailable", unavailable=True)

    corrected_decision = _parse_impact_decision(corrected, pending_tasks)
    if corrected_decision.format_error:
        return ImpactDecision(
            reason=f"{corrected_decision.reason} after JSON correction",
            format_error=True,
            parse_attempts=2,
        )
    return ImpactDecision(
        action=corrected_decision.action,
        task_id=corrected_decision.task_id,
        reason=corrected_decision.reason,
        parse_attempts=2,
    )
