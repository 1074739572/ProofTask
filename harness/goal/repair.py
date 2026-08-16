"""Read-only repair planning for evaluator and final-regression failures."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.agents.runner import run_agent_task
from harness.goal.planner import PLANNER_MAX_ROUNDS

REPAIR_AGENT = "goal_repair_planner"
REPAIR_ACTIONS = frozenset({"implementation_fix", "test_gap", "replan", "blocked"})
GOAL_REGRESSION_ACTIONS = frozenset({"reopen_existing", "create_repair_task", "pause"})
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class RepairDecision:
    action: str
    instructions: str
    assumptions: tuple[dict[str, str], ...] = ()
    summary: str = ""
    error: str | None = None
    unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "instructions": self.instructions,
            "assumptions": [dict(item) for item in self.assumptions],
            "summary": self.summary,
            "error": self.error,
            "unavailable": self.unavailable,
    }


@dataclass(frozen=True)
class GoalRegressionDecision:
    action: str
    task_id: str | None = None
    instructions: str = ""
    summary: str = ""
    error: str | None = None
    unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "task_id": self.task_id,
            "instructions": self.instructions,
            "summary": self.summary,
            "error": self.error,
            "unavailable": self.unavailable,
        }


def _extract_json(text: str) -> str | None:
    for candidate in _FENCE_RE.findall(text or ""):
        if candidate.strip().startswith("{"):
            return candidate.strip()
    start = (text or "").find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_repair_decision(raw: str) -> RepairDecision:
    block = _extract_json(raw)
    if block is None:
        return RepairDecision("blocked", "", error="repair planner returned no JSON", unavailable=True)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return RepairDecision("blocked", "", error=f"invalid repair JSON: {exc}", unavailable=True)
    if not isinstance(data, dict):
        return RepairDecision("blocked", "", error="repair planner output is not an object", unavailable=True)
    action = str(data.get("action") or "blocked")
    if action not in REPAIR_ACTIONS:
        return RepairDecision("blocked", "", error=f"unsupported repair action: {action}", unavailable=True)
    instructions = str(data.get("instructions") or "").strip()[:4_000]
    if action != "blocked" and not instructions:
        return RepairDecision("blocked", "", error="repair action needs instructions", unavailable=True)
    assumptions: list[dict[str, str]] = []
    for item in data.get("assumptions") or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or "").strip()[:600]
        basis = str(item.get("basis") or "").strip()[:600]
        if decision:
            assumptions.append({"decision": decision, "basis": basis})
    return RepairDecision(
        action,
        instructions,
        tuple(assumptions[:8]),
        summary=str(data.get("summary") or "").strip()[:1_000],
    )


def parse_goal_regression_decision(raw: str, task_ids: set[str]) -> GoalRegressionDecision:
    block = _extract_json(raw)
    if block is None:
        return GoalRegressionDecision("pause", error="goal regression planner returned no JSON", unavailable=True)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return GoalRegressionDecision("pause", error=f"invalid goal regression JSON: {exc}", unavailable=True)
    if not isinstance(data, dict):
        return GoalRegressionDecision("pause", error="goal regression planner output is not an object", unavailable=True)
    action = str(data.get("action") or "pause")
    if action not in GOAL_REGRESSION_ACTIONS:
        return GoalRegressionDecision("pause", error=f"unsupported goal regression action: {action}", unavailable=True)
    task_id = str(data.get("task_id") or "").strip() or None
    if action == "reopen_existing" and task_id not in task_ids:
        return GoalRegressionDecision("pause", error="goal regression planner selected an unknown Task", unavailable=True)
    instructions = str(data.get("instructions") or "").strip()[:4_000]
    if action != "pause" and not instructions:
        return GoalRegressionDecision("pause", error="goal regression action needs instructions", unavailable=True)
    return GoalRegressionDecision(
        action=action,
        task_id=task_id,
        instructions=instructions,
        summary=str(data.get("summary") or "").strip()[:1_000],
    )


def build_repair_prompt(state, task, evaluation: dict[str, Any]) -> str:
    return (
        "Plan one autonomous repair. Return ONLY JSON:\n"
        '{"action":"implementation_fix|test_gap|replan|blocked","instructions":"...",'
        '"assumptions":[{"decision":"...","basis":"..."}],"summary":"..."}\n\n'
        "Rules:\n"
        "- The Goal Contract is frozen. Do not remove acceptance cases, weaken tests, or expand the product scope.\n"
        "- Do not ask the user ordinary clarification questions. Resolve ambiguity from the contract, tests, and repository conventions.\n"
        "- implementation_fix changes current Task code; test_gap adds focused coverage; replan changes only remaining work inside the contract; blocked only for authority/external impossibility.\n\n"
        f"Goal Contract:\n{json.dumps(state.goal_contract, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Task: {task.id} {task.subject}\nBehavior: {task.description}\n"
        f"Acceptance cases: {json.dumps(task.acceptance_cases, ensure_ascii=False)}\n"
        f"Verification: {json.dumps(task.verification_spec, ensure_ascii=False)}\n"
        f"Goal final-verification evidence: {json.dumps(getattr(state, 'final_verification', None), ensure_ascii=False)[:8000]}\n"
        f"Latest task/clean-gate error: {task.last_error or '(none)'}\n"
        f"Task-start snapshot: {task.start_snapshot or '(not recorded)'}\n"
        f"Task-start diff: {(task.start_diff or '(clean)').strip()[:8000]}\n"
        f"Evaluation: {json.dumps(evaluation, ensure_ascii=False)[:12000]}"
    )


def plan_task_repair(
    state,
    task,
    evaluation: dict[str, Any],
    *,
    cwd: str,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
    runner=None,
) -> RepairDecision:
    invoke = runner or run_agent_task
    try:
        raw = invoke(
            description=f"repair plan for task {task.id}",
            prompt=build_repair_prompt(state, task, evaluation),
            agent_type=REPAIR_AGENT,
            cwd=cwd,
            max_rounds=PLANNER_MAX_ROUNDS,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception as exc:
        return RepairDecision("blocked", "", error=f"repair planner failed: {type(exc).__name__}: {exc}", unavailable=True)
    return parse_repair_decision(raw)


def plan_goal_regression_repair(
    state,
    tasks: list,
    *,
    cwd: str,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
    runner=None,
) -> GoalRegressionDecision:
    """Analyze a failed full-suite gate before mutating the Task plan."""
    task_rows = [
        {
            "id": task.id,
            "subject": task.subject,
            "behavior": task.description,
            "status": task.status,
            "selectors": list((task.verification_spec or {}).get("selectors") or []),
        }
        for task in tasks
    ]
    prompt = (
        "Analyze one failed Goal-level pytest verification. Return ONLY JSON:\n"
        '{"action":"reopen_existing|create_repair_task|pause","task_id":"existing task id or null",'
        '"instructions":"...","summary":"..."}\n\n'
        "Rules:\n"
        "- First prefer reopen_existing when one completed Goal Task owns or caused the regression.\n"
        "- create_repair_task is allowed only for a concrete, collected FAILED pytest selector that cannot reasonably belong to an existing Task. It creates implementation work only; never request generated tests.\n"
        "- pause for interrupted runs, unavailable infrastructure, unclear ownership, or missing concrete evidence.\n"
        "- Do not change the Goal contract or weaken a test.\n\n"
        f"Goal contract:\n{json.dumps(state.goal_contract, ensure_ascii=False)[:4000]}\n\n"
        f"Full verification evidence:\n{json.dumps(state.final_verification, ensure_ascii=False)[:12000]}\n\n"
        f"Existing Goal Tasks:\n{json.dumps(task_rows, ensure_ascii=False)[:12000]}"
    )
    invoke = runner or run_agent_task
    try:
        raw = invoke(
            description=f"analyze full verification failure for goal {state.id}",
            prompt=prompt,
            agent_type=REPAIR_AGENT,
            cwd=cwd,
            max_rounds=PLANNER_MAX_ROUNDS,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception as exc:
        return GoalRegressionDecision("pause", error=f"goal regression planner failed: {type(exc).__name__}: {exc}", unavailable=True)
    if str(getattr(stats, "stop_reason", "") or "") == "provider_error":
        return GoalRegressionDecision("pause", error="goal regression planner provider unavailable", unavailable=True)
    return parse_goal_regression_decision(raw, {task.id for task in tasks})
