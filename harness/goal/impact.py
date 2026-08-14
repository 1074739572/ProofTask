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
        block = _extract_json(raw)
        data = json.loads(block or "{}")
    except Exception:
        return ImpactDecision()
    action = str(data.get("action") or "none")
    task_id = str(data.get("task_id") or "")
    valid_ids = {task.id for task in pending_tasks}
    if action == "add_tests" and task_id in valid_ids:
        return ImpactDecision("add_tests", task_id, str(data.get("reason") or "")[:1200])
    return ImpactDecision()
