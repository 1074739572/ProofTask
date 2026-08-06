"""/goal CLI command parsing and handlers (L6).

Command surface (docs/goal-mode-mvp-spec.md §4):

    /goal --verify "<command>" [--max-rounds N] [--max-attempts N]
          [--max-failures N] [--timeout N] -- <target>
    /goal status | pause | resume | cancel

MVP deliberately does NOT infer the verification command from HARNESS.md or
the model: a bare ``/goal <target>`` returns a usage error.
"""

from __future__ import annotations

import shlex
from typing import Any

GOAL_USAGE = (
    "Usage:\n"
    '  /goal --verify "<command>" -- <target>      start an autonomous goal\n'
    "  /goal --verify \"<cmd>\" --max-rounds 20 --timeout 1800 --max-failures 3 -- <target>\n"
    "  /goal status     view the current goal\n"
    "  /goal pause      pause (cooperative; current round finishes)\n"
    "  /goal resume     continue a paused goal\n"
    "  /goal cancel     cancel (terminal; history is kept)"
)


def parse_goal_subcommand(query: str) -> str | None:
    """'status' / 'pause' / 'resume' / 'cancel' / 'start', or None when the
    query is not a /goal command."""
    text = query.strip()
    if not text.lower().startswith("/goal"):
        return None
    rest = text[len("/goal") :].strip()
    try:
        tokens = shlex.split(rest) if rest else []
    except ValueError:
        return "usage"
    if not tokens:
        return "usage"
    if tokens[0] in ("status", "pause", "resume", "cancel"):
        return tokens[0]
    return "start"


def parse_goal_command(query: str) -> dict[str, Any]:
    """Parse a /goal query into an action dict (never raises).

    Returns ``{"action": ...}`` with ``usage`` actions carrying an ``error``
    message, and ``start`` actions carrying verify/limits/target.
    """
    action = parse_goal_subcommand(query)
    if action is None:
        return {"action": "usage", "error": "not a /goal command"}
    text = query.strip()
    rest = text[len("/goal") :].strip()
    try:
        tokens = shlex.split(rest) if rest else []
    except ValueError as exc:
        return {"action": "usage", "error": f"unparseable arguments: {exc}"}
    if not tokens:
        return {"action": "usage", "error": GOAL_USAGE}
    if tokens[0] in ("status", "pause", "resume", "cancel"):
        if len(tokens) > 1:
            return {"action": "usage", "error": f"/goal {tokens[0]} takes no arguments"}
        return {"action": tokens[0]}

    # Everything after `--` is target text (even tokens that look like flags).
    if "--" not in tokens:
        return {"action": "usage", "error": GOAL_USAGE}
    idx = tokens.index("--")
    opts, target_parts = tokens[:idx], tokens[idx + 1 :]
    target = " ".join(target_parts).strip()
    if not target:
        return {"action": "usage", "error": "goal target must not be empty"}

    verify: str | None = None
    limits: dict[str, int] = {}
    i = 0
    while i < len(opts):
        tok = opts[i]
        if tok == "--verify":
            if i + 1 >= len(opts):
                return {"action": "usage", "error": "--verify requires a command"}
            verify = opts[i + 1]
            i += 2
        elif tok in ("--max-rounds", "--max-attempts", "--max-failures", "--timeout"):
            if i + 1 >= len(opts):
                return {"action": "usage", "error": f"{tok} requires a number"}
            try:
                value = int(opts[i + 1])
            except ValueError:
                return {"action": "usage", "error": f"{tok} requires an integer, got {opts[i + 1]!r}"}
            if value <= 0:
                return {"action": "usage", "error": f"{tok} must be positive"}
            limits[tok[2:].replace("-", "_")] = value  # --max-rounds -> max_rounds
            i += 2
        else:
            return {"action": "usage", "error": f"unknown option {tok!r}"}
    if not verify:
        return {"action": "usage", "error": GOAL_USAGE}
    return {
        "action": "start",
        "verify": verify,
        "limits": limits,
        "target": target,
    }


def handle_goal_command(query: str, history: list, context: dict, binding: Any) -> str:
    """Dispatch a /goal command to the runner layer; returns user-facing text."""
    from harness.goal import runner
    from harness.goal.models import GoalStatus
    from harness.goal.store import GoalStoreError

    cmd = parse_goal_command(query)
    action = cmd["action"]
    if action == "usage":
        return cmd.get("error") or GOAL_USAGE
    try:
        if action == "status":
            return runner.get_goal_status()
        if action == "pause":
            return _handle_pause(runner)
        if action == "cancel":
            return _handle_cancel(runner)
        if action == "resume":
            return _handle_resume(runner, history, context, binding)
        return _handle_start(runner, cmd, history, context, binding)
    except GoalStoreError as exc:
        return f"Goal state is {exc.code}: {exc}"
    except runner.GoalNotRunningError as exc:
        return str(exc)
    except runner.GoalBusyError as exc:
        return str(exc)


def _handle_pause(runner) -> str:
    state = runner.pause_goal()
    return (
        f"Goal paused [paused]\n"
        f"  Phase: {state.phase} (current round finishes, then it stops)\n"
        f"  Resume: /goal resume"
    )


def _handle_cancel(runner) -> str:
    state = runner.cancel_goal()
    return (
        f"Goal stopped [cancelled]\n"
        f"  Reason: {state.stop_reason}\n"
        f"  Feature: {state.feature_id or '-'}\n"
        f"  History kept under .project/goal-history/"
    )


def _handle_resume(runner, history: list, context: dict, binding: Any) -> str:
    state = runner.resume_goal(history=history, context=context, binding=binding)
    return (
        f"Goal resumed: {state.id} [select_feature]\n"
        f"  Target: {state.target[:80]}\n"
        f"  Attempt: {state.attempts}/{state.max_attempts}"
    )


def _start_precondition_note(request: Any) -> str | None:
    """Start preconditions (spec §4.2). Returns a rejection note or None."""
    import subprocess

    from harness.loop import agent_lock
    from harness.settings import get_workdir
    from harness.verification import check_verification_command

    if agent_lock.locked():
        return (
            "Cannot start /goal while an agent turn is running — "
            "wait for it to finish first."
        )
    workspace = get_workdir()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return (
            f"Workspace {workspace} is not a valid git repository with a "
            "resolvable HEAD — /goal needs git snapshots for stale/no-progress "
            "detection."
        )
    decision = check_verification_command(request.verification)
    if not decision.allowed:
        return f"Verification command rejected by policy: {decision.reason}"
    return None


def _handle_start(runner, cmd: dict, history: list, context: dict, binding: Any) -> str:
    from harness.goal.models import GoalState
    from harness.goal.runner import GoalRequest

    request = GoalRequest(
        target=cmd["target"],
        verification=cmd["verify"],
        **cmd["limits"],
    )
    note = _start_precondition_note(request)
    if note is not None:
        return note
    state = runner.start_goal(request, history=history, context=context, binding=binding)
    assert isinstance(state, GoalState)
    return (
        f"Goal started: {state.id} [INITIALIZE]\n"
        f"  Target: {state.target}\n"
        f"  Verification: {state.verification}\n"
        f"  Limits: rounds/attempt={state.max_rounds_per_attempt}, "
        f"attempts={state.max_attempts}, failures={state.max_consecutive_failures}, "
        f"timeout={state.max_duration_seconds}s"
    )
