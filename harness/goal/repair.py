"""Read-only repair planning for evaluator and final-regression failures."""

from __future__ import annotations

import json
import re
import hashlib
from dataclasses import replace
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.agents.runner import run_agent_task
from harness.goal.planner import PLANNER_MAX_ROUNDS

REPAIR_AGENT = "goal_repair_planner"
REPAIR_ACTIONS = frozenset({"implementation_fix", "test_gap", "replan", "blocked"})
GOAL_REGRESSION_ACTIONS = frozenset({"reopen_existing", "create_repair_task", "pause"})
MAX_REPAIR_RAW_OUTPUT_TAIL = 2_000
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_HOST_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|/(?:Users|home|tmp|var|private)/)[^\s\"']*",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;\]}]+"
)


@dataclass(frozen=True)
class RepairDecision:
    action: str
    instructions: str
    assumptions: tuple[dict[str, str], ...] = ()
    summary: str = ""
    error: str | None = None
    unavailable: bool = False
    format_fallback: bool = False
    raw_output_tail: str = ""
    raw_output_sha256: str = ""
    correction_output_tail: str = ""
    correction_output_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "instructions": self.instructions,
            "assumptions": [dict(item) for item in self.assumptions],
            "summary": self.summary,
            "error": self.error,
            "unavailable": self.unavailable,
            "format_fallback": self.format_fallback,
            "raw_output_tail": self.raw_output_tail,
            "raw_output_sha256": self.raw_output_sha256,
            "correction_output_tail": self.correction_output_tail,
            "correction_output_sha256": self.correction_output_sha256,
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


def _repair_output_audit(raw: str | None) -> tuple[str, str]:
    text = str(raw or "")
    tail = text[-MAX_REPAIR_RAW_OUTPUT_TAIL:]
    redacted = _HOST_PATH_RE.sub("[redacted-host-path]", tail)
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1=[redacted]", redacted)
    return redacted, hashlib.sha256(
        text.encode("utf-8", errors="replace")
    ).hexdigest()


def _with_repair_output_audit(
    decision: RepairDecision,
    *,
    raw: str | None,
    corrected: str | None = None,
) -> RepairDecision:
    raw_tail, raw_hash = _repair_output_audit(raw)
    corrected_tail, corrected_hash = _repair_output_audit(corrected)
    return replace(
        decision,
        raw_output_tail=raw_tail,
        raw_output_sha256=raw_hash,
        correction_output_tail=corrected_tail,
        correction_output_sha256=corrected_hash if corrected is not None else "",
    )


def _repair_handoff(state, task) -> dict[str, Any]:
    """Return only the current Task's bounded durable execution facts."""
    from harness.goal.memory import load_handoff

    handoff = load_handoff(state)
    task_row = handoff.get("task") if isinstance(handoff.get("task"), dict) else {}
    if task_row.get("id") != task.id:
        return {}
    execution = handoff.get("execution") if isinstance(handoff.get("execution"), dict) else {}
    failure = handoff.get("failure") if isinstance(handoff.get("failure"), dict) else {}
    def belongs_to_current_task(section: dict[str, Any]) -> bool:
        return (
            section.get("goal_id") == state.id
            and section.get("task_id") == task.id
        )

    # The outer handoff Task is not sufficient: an interrupted write from a
    # previous Task can otherwise be enclosed by a newer top-level snapshot.
    if not belongs_to_current_task(execution):
        execution = {}
    if not belongs_to_current_task(failure):
        failure = {}
    return {
        "artifact": f".project/goal-memory/{state.id}/handoff.json",
        "worker_summary": str(execution.get("worker_summary") or "")[:4_000],
        "worker_summary_missing": bool(execution.get("worker_summary_missing")),
        "stop_reason": str(execution.get("stop_reason") or ""),
        "write_paths": list(execution.get("write_paths") or [])[-12:],
        "write_outcomes": list(execution.get("write_outcomes") or [])[-12:],
        "tool_errors": list(execution.get("tool_errors") or [])[-8:],
        "failure": {
            "classification": failure.get("classification"),
            "route": failure.get("route"),
            "summary": str(failure.get("summary") or "")[:2_000],
            "task_error": str(failure.get("task_error") or "")[:4_000],
            "verification": failure.get("verification") if isinstance(failure.get("verification"), dict) else {},
            "attempts": list(failure.get("attempts") or [])[-4:],
            "retry": failure.get("retry") if isinstance(failure.get("retry"), dict) else {},
            "next_action": str(failure.get("next_action") or "")[:1_000],
        },
    }


def fallback_repair_decision(
    error: str | None,
    *,
    route: str = "implementation_fix",
) -> RepairDecision:
    """Preserve the deterministic repair route when planner JSON is unusable."""
    detail = str(error or "repair planner returned unusable JSON").strip()
    action = route if route in REPAIR_ACTIONS else "implementation_fix"
    instructions_by_action = {
        "implementation_fix": (
            "Resume the current Task and implement the smallest code fix supported by the "
            "existing verification evidence and frozen Goal Contract. Do not regenerate, "
            "remove, or weaken tests. Run the bound verification before reporting completion."
        ),
        "test_gap": (
            "Prepare focused bound tests for the uncovered acceptance behavior identified by "
            "the existing evidence. Do not weaken or replace existing tests."
        ),
        "replan": (
            "Recompile the unfinished Task contracts from the frozen Goal Contract and current "
            "evaluation evidence. Split cross-task integration behavior into ordered, independently "
            "verifiable Tasks before resuming implementation."
        ),
        "blocked": "",
    }
    return RepairDecision(
        action=action,
        instructions=instructions_by_action[action],
        assumptions=(
            {
                "decision": f"Continue with the deterministic {action} route without a model-generated plan.",
                "basis": "The repair planner returned unusable structured output; Task evidence and test bindings remain available.",
            },
        ),
        summary=f"Repair planner returned invalid JSON; deterministic fallback selected {action}.",
        error=f"repair planner format fallback: {detail}",
        unavailable=False,
        format_fallback=True,
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
    handoff = _repair_handoff(state, task)
    return (
        "Plan one autonomous repair. Return ONLY JSON:\n"
        '{"action":"implementation_fix|test_gap|replan|blocked","instructions":"...",'
        '"assumptions":[{"decision":"...","basis":"..."}],"summary":"..."}\n\n'
        "Rules:\n"
        "- The Goal Contract is frozen. Do not remove acceptance cases, weaken tests, or expand the product scope.\n"
        "- Do not ask the user ordinary clarification questions. Resolve ambiguity from the contract, tests, and repository conventions.\n"
        "- implementation_fix changes current Task code; test_gap adds focused coverage; replan replaces only unfinished Task contracts inside the frozen contract after planner review; blocked only for authority/external impossibility.\n"
        "- A failed test, empty worker summary, timeout, or tool/runtime error is an implementation blocker, not a reason to replan. Choose replan only when the evaluator explicitly marks a contract/scope/dependency mismatch, or when Evaluation contains a machine-generated stalled_task_review with repeated failed bound-verification evidence.\n"
        "- Treat the evaluator route as a diagnosis to verify, not as vague advice. Do not return implementation_fix after repeated no-progress evidence without explaining the changed direction.\n\n"
        f"Goal Contract:\n{json.dumps(state.goal_contract, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Task: {task.id} {task.subject}\nBehavior: {task.description}\n"
        f"Task scope: {json.dumps({'primary_write': getattr(task, 'primary_write', []), 'planned_new': getattr(task, 'planned_new', []), 'conditional_write': getattr(task, 'conditional_write', []), 'read_envelope': getattr(task, 'read_envelope', [])}, ensure_ascii=False)}\n"
        f"Acceptance cases: {json.dumps(task.acceptance_cases, ensure_ascii=False)}\n"
        f"Verification: {json.dumps(task.verification_spec, ensure_ascii=False)}\n"
        f"Goal final-verification evidence: {json.dumps(getattr(state, 'final_verification', None), ensure_ascii=False)[:8000]}\n"
        f"Latest task/clean-gate error: {task.last_error or '(none)'}\n"
        f"Task-start snapshot: {task.start_snapshot or '(not recorded)'}\n"
        f"Task-start diff: {(task.start_diff or '(clean)').strip()[:8000]}\n"
        f"Evaluation: {json.dumps(evaluation, ensure_ascii=False)[:12000]}\n"
        f"Durable failure handoff: {json.dumps(handoff, ensure_ascii=False)}"
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
    prompt = build_repair_prompt(state, task, evaluation)
    try:
        raw = invoke(
            description=f"repair plan for task {task.id}",
            prompt=prompt,
            agent_type=REPAIR_AGENT,
            cwd=cwd,
            max_rounds=PLANNER_MAX_ROUNDS,
            tools_override=(),
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception as exc:
        return RepairDecision("blocked", "", error=f"repair planner failed: {type(exc).__name__}: {exc}", unavailable=True)
    if stats is not None and getattr(stats, "stop_reason", "") in {
        "provider_error", "configuration_error", "deadline", "cancelled",
    }:
        return RepairDecision(
            "blocked", "",
            error=f"repair planner stopped: {stats.stop_reason}",
            unavailable=True,
        )
    decision = parse_repair_decision(raw)
    if not decision.unavailable or not (decision.error or "").startswith((
        "repair planner returned no JSON",
        "invalid repair JSON:",
        "repair planner output is not an object",
        "unsupported repair action:",
        "repair action needs instructions",
    )):
        return _with_repair_output_audit(decision, raw=raw)

    # A completed model turn with malformed structured output is not a repair
    # attempt. Give it one bounded correction turn before the runner pauses;
    # the original evidence is retained in the correction prompt.
    correction_stats = type(stats)() if stats is not None else None
    try:
        corrected = invoke(
            description=f"repair plan for task {task.id} (JSON correction)",
            prompt=(
                prompt
                + "\n\nYour previous response did not match the required JSON contract. "
                "Return ONLY one valid JSON object, with an allowed action and non-empty instructions when required. "
                "Previous response:\n"
                + str(raw)[-2_000:]
            ),
            agent_type=REPAIR_AGENT,
            cwd=cwd,
            max_rounds=PLANNER_MAX_ROUNDS,
            tools_override=(),
            cancel_check=cancel_check,
            deadline=deadline,
            stats=correction_stats,
        )
    except Exception as exc:
        return RepairDecision("blocked", "", error=f"repair planner correction failed: {type(exc).__name__}: {exc}", unavailable=True)
    if stats is not None and correction_stats is not None:
        stats.llm_rounds += correction_stats.llm_rounds
        if correction_stats.stop_reason:
            stats.stop_reason = correction_stats.stop_reason
    if correction_stats is not None and correction_stats.stop_reason in {
        "provider_error", "configuration_error", "deadline", "cancelled",
    }:
        return RepairDecision(
            "blocked", "",
            error=f"repair planner correction stopped: {correction_stats.stop_reason}",
            unavailable=True,
        )
    corrected_decision = parse_repair_decision(corrected)
    if corrected_decision.unavailable:
        return _with_repair_output_audit(
            fallback_repair_decision(
                f"{corrected_decision.error} after JSON correction",
                route=str(evaluation.get("route") or "implementation_fix"),
            ),
            raw=raw,
            corrected=corrected,
        )
    return _with_repair_output_audit(corrected_decision, raw=raw, corrected=corrected)


def plan_goal_regression_repair(
    state,
    tasks: list,
    *,
    cwd: str,
    adapter_id: str = "pytest",
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
        f"Analyze one failed Goal-level {adapter_id} verification. Return ONLY JSON:\n"
        '{"action":"reopen_existing|create_repair_task|pause","task_id":"existing task id or null",'
        '"instructions":"...","summary":"..."}\n\n'
        "Rules:\n"
        "- First prefer reopen_existing when one completed Goal Task owns or caused the regression.\n"
        "- create_repair_task is allowed only for one concrete, collected failing selector from the active verification adapter that cannot reasonably belong to an existing Task. It creates implementation work only; never request generated tests.\n"
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
    if str(getattr(stats, "stop_reason", "") or "") in {"provider_error", "configuration_error"}:
        return GoalRegressionDecision("pause", error="goal regression planner provider unavailable", unavailable=True)
    return parse_goal_regression_decision(raw, {task.id for task in tasks})
