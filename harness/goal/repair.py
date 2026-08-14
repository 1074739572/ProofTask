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
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class RepairDecision:
    action: str
    instructions: str
    assumptions: tuple[dict[str, str], ...] = ()
    summary: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "instructions": self.instructions,
            "assumptions": [dict(item) for item in self.assumptions],
            "summary": self.summary,
            "error": self.error,
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
        return RepairDecision("blocked", "", error="repair planner returned no JSON")
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return RepairDecision("blocked", "", error=f"invalid repair JSON: {exc}")
    if not isinstance(data, dict):
        return RepairDecision("blocked", "", error="repair planner output is not an object")
    action = str(data.get("action") or "blocked")
    if action not in REPAIR_ACTIONS:
        return RepairDecision("blocked", "", error=f"unsupported repair action: {action}")
    instructions = str(data.get("instructions") or "").strip()[:4_000]
    if action != "blocked" and not instructions:
        return RepairDecision("blocked", "", error="repair action needs instructions")
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
        return RepairDecision("blocked", "", error=f"repair planner failed: {type(exc).__name__}: {exc}")
    return parse_repair_decision(raw)
