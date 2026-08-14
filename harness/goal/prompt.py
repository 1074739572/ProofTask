"""Instructions for a single Task attempt inside Goal mode."""

from __future__ import annotations

from typing import Any


def build_goal_act_prompt(state: Any, task: Any) -> str:
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
    if task.last_error:
        lines.append(f"Last verification error: {task.last_error}")
    if tail:
        lines += ["Last verification output:", tail[:2000]]
    lines += [
        "Requirements:",
        "- Stay within this Task and its acceptance cases.",
        "- Do not delete, weaken, skip, or edit a bound test just to pass it.",
        "- Do not claim completion in prose; the runner runs the bound tests.",
        "- When the work is ready, summarize and stop.",
    ]
    return "\n".join(lines)
