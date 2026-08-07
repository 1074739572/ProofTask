"""ACT-stage instruction prompt for the goal agent (L6).

The runner appends this as one user message before each ``agent_loop`` call.
It must steer the model to work on exactly one feature (WIP=1) and never
claim completion — machine verification is authoritative.
"""

from __future__ import annotations

from typing import Any


def build_goal_act_prompt(state: Any, feature: Any) -> str:
    """Build the ACT prompt for one attempt (spec §7.6 + L6 v2 scope line)."""
    error_line = f"\n  Current error: {feature.last_error}" if getattr(feature, "last_error", None) else ""
    verify_tail = ""
    evidence = getattr(feature, "evidence", None) or []
    if evidence:
        last = evidence[-1]
        tail = str(last.get("stdout_tail") or "").strip()
        if tail:
            verify_tail = (
                "\n  Last verification output (read-only):\n"
                + "\n".join(f"    {line}" for line in tail.splitlines()[:12])
            )
    scope_line = ""
    if getattr(state, "feature_ids", None):
        total = len(state.feature_ids)
        index = state.feature_ids.index(feature.id) + 1 if feature.id in state.feature_ids else 0
        if total > 1:
            scope_line = f"\nFeature scope: {index}/{total} (dependencies done; work only this one)"
    lines = [
        "You are working on a single feature for an autonomous goal. "
        "Work only on this feature until it is fixed — then stop.",
        "",
        f"Goal target: {state.target}",
        f"Verification command: {state.verification}",
        f"Feature: {feature.id} ({feature.name})",
        f"Feature behavior: {feature.behavior}",
        f"Feature state: {feature.state}",
        error_line,
        verify_tail,
        scope_line,
        f"Attempt: {state.attempts + 1}/{state.max_attempts}",
        "",
        "Requirements:",
        "- Work ONLY on this feature (WIP=1). Do not touch unrelated code, and "
        "do not create or claim other features or tasks.",
        "- Do NOT claim completion in your final reply. The machine verification "
        "command runs automatically after your turn and is authoritative.",
        "- Do NOT modify .features/ files by hand.",
        "- Do NOT call create_feature/claim_feature/verify_feature/"
        "evaluate_feature/complete_task/clear_tasks — the goal runner handles "
        "feature orchestration for you.",
        "- You may run focused tests to check your work, but only the runner's "
        "verification decides pass/fail.",
        "- When you are done editing, summarize what you changed and stop.",
    ]
    return "\n".join(lines)
