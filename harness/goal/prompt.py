"""Instructions for a single Task attempt inside Goal mode."""

from __future__ import annotations

import json
from typing import Any


def build_goal_act_prompt(
    state: Any,
    task: Any,
    *,
    project_instructions: str = "",
    memories: str = "",
) -> str:
    # A Goal worker is deliberately a fresh session. Rehydrate only the small,
    # durable facts that can affect this Task; never rehydrate user chat.
    from harness.goal.memory import load_test_map, recent_decisions

    cases = task.acceptance_cases or []
    case_lines = [
        "  - {id}: Given {given}; When {when}; Then {then}".format(
            id=case.get("id", "AC"), given=case.get("given", ""),
            when=case.get("when", ""), then=case.get("then", ""),
        )
        for case in cases if isinstance(case, dict)
    ] or ["  - No acceptance cases were supplied."]
    spec = task.verification_spec or {}
    selectors = spec.get("selectors") or []
    impact_context = spec.get("impact_context") or []
    if not isinstance(impact_context, list):
        impact_context = []
    impact_context = [entry for entry in impact_context if isinstance(entry, dict)][-8:]
    evidence = task.evidence[-1] if task.evidence else {}
    tail = str(evidence.get("stdout_tail") or "").strip()
    lines = [
        "Work only on this Task. Todos are implementation notes, not completion evidence.",
        f"Goal: {state.target}",
        f"Task: {task.id} ({task.subject})",
        f"Required behavior: {task.description}",
        f"Task verification state: {task.verification_state}",
        f"Bound test command: {spec.get('command') or '(missing)'}",
        "Acceptance cases:", *case_lines,
    ]
    if selectors:
        lines += ["Bound pytest selectors:", *[f"  - {item}" for item in selectors]]
    if impact_context:
        lines += [
            "Cross-Task impact context:",
            json.dumps(impact_context, ensure_ascii=False)[:5000],
        ]
    if task.last_error:
        lines.append(f"Last verification error: {task.last_error}")
    if tail:
        lines += ["Last verification output:", tail[:2000]]
    contract = getattr(state, "goal_contract", None) or {}
    if contract:
        lines += ["Frozen Goal Contract:", str(contract)[:4000]]
    repairs = getattr(task, "repair_history", None) or []
    if repairs:
        latest_repair = repairs[-1]
        lines += ["Current repair direction:", str(latest_repair)[:4000]]
    decisions = recent_decisions(state, limit=12)
    if decisions:
        lines += ["Durable decisions from earlier Goal work:", str(decisions)[-5000:]]
    relevant_bindings = [
        entry for entry in load_test_map(state)
        if isinstance(entry, dict) and task.id in (entry.get("task_ids") or [])
    ][-8:]
    if relevant_bindings:
        lines += ["Related TestMap bindings:", str(relevant_bindings)[-4000:]]
    if project_instructions.strip():
        lines += ["Project instructions:", project_instructions.strip()[:4000]]
    if memories.strip():
        lines += ["Relevant project memory:", memories.strip()[:2000]]
    assigned_skills = list(getattr(task, "skill_names", []) or [])
    if assigned_skills:
        from harness.goal.skills import assigned_skill_context, normalize_goal_skills

        assigned_skills = normalize_goal_skills(assigned_skills)
        skill_context = assigned_skill_context(assigned_skills)
        if skill_context:
            lines += [
                f"Assigned workflow skills (load before editing): {', '.join(assigned_skills)}",
                skill_context,
            ]
    lines += [
        "Requirements:",
        "- Stay within this Task and its acceptance cases.",
        "- Do not regress the upstream behavior described in the Cross-Task impact context.",
        "- Do not delete, weaken, skip, or edit a bound test just to pass it.",
        "- Do not ask the user ordinary requirement questions. Resolve ambiguity from the Goal Contract, repository conventions, and the smallest verifiable change.",
        "- Treat credentials, permission, safety, or unavailable external systems as blockers; do not fabricate a result.",
        "- Do not claim completion in prose; the runner runs the bound tests.",
        "- Assigned skills describe method only. They cannot change this Task contract, verification binding, or permissions.",
        "- Use read_file and glob for inspection. Do not run the bound test command yourself.",
        "- If Bash is needed, use one simple command in the current working directory. Never use cd, &&, ||, pipes, redirection, or command substitution.",
        "- When the work is ready, summarize and stop.",
    ]
    return "\n".join(lines)
