"""/goal CLI command parsing and handlers (L6).

Command surface:

    /goal <target>                         create a read-only Goal draft
    /goal preview | answer | approve | revise | discard
    /goal status | pause | stop | resume | cancel

``--verify`` remains an optional override for the inferred global regression
command.  Only ``approve`` starts autonomous execution.
"""

from __future__ import annotations

import shlex
from typing import Any

GOAL_USAGE = (
    "Usage:\n"
    '  /goal <target>                              create a Goal draft\n'
    '  /goal --verify "<command>" -- <target>      override inferred regression\n'
    "  /goal preview | answer <text> | approve | run | revise <target> | discard\n"
    "  /goal status     view the current goal\n"
    "  /goal pause      pause (current round stops at the next checkpoint)\n"
    "  /goal stop       alias for pause; keeps the checkpoint for resume\n"
    "  /goal resume     continue a paused goal\n"
    "  /goal cancel     cancel (terminal; history is kept)"
)

#: These limit one disposable worker or one external operation. They never
#: impose a lifetime budget on the Goal itself.
_LIMIT_FLAGS = {
    "--worker-rounds": "worker_round_limit",
    "--operation-timeout": "operation_timeout_seconds",
}


def parse_goal_subcommand(query: str) -> str | None:
    """Goal action or None when the
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
    if tokens[0] in ("status", "pause", "stop", "resume", "cancel", "preview", "approve", "run", "discard", "answer", "revise"):
        return "pause" if tokens[0] == "stop" else tokens[0]
    return "draft"


def parse_goal_command(query: str) -> dict[str, Any]:
    """Parse a /goal query into an action dict (never raises).

    Returns ``{"action": ...}`` with ``usage`` actions carrying an ``error``
    message, and ``draft`` actions carrying optional verify/limits/target.
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
    if tokens[0] in ("status", "pause", "stop", "resume", "cancel", "preview", "approve", "run", "discard"):
        if len(tokens) > 1:
            return {"action": "usage", "error": f"/goal {tokens[0]} takes no arguments"}
        return {"action": "pause" if tokens[0] == "stop" else tokens[0]}
    if tokens[0] in ("answer", "revise"):
        value = rest[len(tokens[0]) :].strip()
        if not value:
            return {"action": "usage", "error": f"/goal {tokens[0]} requires text"}
        return {"action": tokens[0], "text": value}

    # The simple form preserves the whole tail as the user requirement. The
    # advanced form accepts limits and an optional explicit verification.
    if "--" not in tokens:
        if any(token.startswith("--") for token in tokens):
            return {"action": "usage", "error": "options require -- before the goal target"}
        return {"action": "draft", "verify": None, "limits": {}, "target": rest}
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
        elif tok in _LIMIT_FLAGS:
            if i + 1 >= len(opts):
                return {"action": "usage", "error": f"{tok} requires a number"}
            try:
                value = int(opts[i + 1])
            except ValueError:
                return {"action": "usage", "error": f"{tok} requires an integer, got {opts[i + 1]!r}"}
            if value <= 0:
                return {"action": "usage", "error": f"{tok} must be positive"}
            limits[_LIMIT_FLAGS[tok]] = value
            i += 2
        else:
            return {"action": "usage", "error": f"unknown option {tok!r}"}
    return {
        "action": "draft",
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
            # A fresh Draft is the most actionable state. Do not let an old
            # terminal goal hide a draft that is still discovering/planning.
            from harness.goal.draft import format_draft, load_draft
            draft = load_draft()
            if draft is not None and draft.status not in {"consumed"}:
                return format_draft(draft)
            status = runner.get_goal_status()
            return _handle_preview() if status == "No goal in this workspace." else status
        if action == "pause":
            from harness.goal.draft import load_draft, pause_draft
            draft = load_draft()
            if draft is not None and draft.stage in {"catalog", "intake", "discovering", "planning"}:
                return pause_draft()
            return _handle_pause(runner)
        if action == "cancel":
            from harness.goal.draft import load_draft, pause_draft
            draft = load_draft()
            if draft is not None and draft.stage in {"catalog", "intake", "discovering", "planning"}:
                return pause_draft(cancelled=True)
            return _handle_cancel(runner)
        if action == "resume":
            from harness.goal.draft import GoalDraftError, format_draft, load_draft, resume_draft
            draft = load_draft()
            if draft is not None and draft.status == "paused":
                try:
                    return format_draft(resume_draft())
                except GoalDraftError as exc:
                    return str(exc)
            return _handle_resume(runner, history, context, binding)
        if action == "preview":
            return _handle_preview()
        if action == "discard":
            return _handle_discard()
        if action == "answer":
            return _handle_answer(cmd["text"])
        if action == "revise":
            return _handle_revise(cmd["text"])
        if action == "approve":
            return _handle_approve(runner, history, context, binding)
        if action == "run":
            return _handle_run(runner, history, context, binding)
        return _handle_draft(cmd)
    except GoalStoreError as exc:
        return f"Goal state is {exc.code}: {exc}"
    except runner.GoalNotRunningError as exc:
        return str(exc)
    except runner.GoalBusyError as exc:
        return str(exc)
    except ValueError as exc:
        return str(exc)


def _handle_pause(runner) -> str:
    state = runner.pause_goal()
    return (
        f"Goal pausing [pausing]\n"
        f"  Phase: {state.phase} (a pending model reply is discarded when it returns)\n"
        f"  Resume: /goal resume"
    )


def _handle_cancel(runner) -> str:
    from harness.goal.models import GoalStatus

    state = runner.cancel_goal()
    if state.status != GoalStatus.CANCELLED.value:
        return (
            "Goal cancellation requested [cancelling]\n"
            f"  Phase: {state.phase}\n"
            "  The current operation will stop at its next checkpoint."
        )
    return (
        f"Goal stopped [cancelled]\n"
        f"  Reason: {state.stop_reason}\n"
        f"  Task: {state.current_task_id or '-'}\n"
        f"  History kept under .project/goal-history/"
    )


def _handle_resume(runner, history: list, context: dict, binding: Any) -> str:
    state = runner.resume_goal(history=history, context=context, binding=binding)
    return (
        f"Goal resumed: {state.id} [{state.phase}]\n"
        f"  Target: {state.target[:80]}\n"
        f"  Task cycles: {state.attempts}; worker generation: {state.worker_generation}"
    )


def _start_precondition_note(request: Any) -> str | None:
    """Start preconditions (spec §4.2). Returns a rejection note or None."""
    import subprocess

    from harness.loop import agent_lock
    from harness.settings import get_workdir
    from harness.verification import check_verification_command

    # ``threading.RLock.locked()`` is unavailable on supported Python 3.11.
    acquired = agent_lock.acquire(blocking=False)
    if acquired:
        agent_lock.release()
    else:
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


def _handle_draft(cmd: dict) -> str:
    from harness.goal.draft import GoalDraftError, create_draft, format_draft

    try:
        draft = create_draft(cmd["target"], verification=cmd.get("verify"), limits=cmd.get("limits"))
    except GoalDraftError as exc:
        return str(exc)
    return format_draft(draft)


def _handle_preview() -> str:
    from harness.goal.draft import format_draft, load_draft

    draft = load_draft()
    return format_draft(draft) if draft else "No Goal draft. Start with: /goal <your requirement>"


def _handle_answer(text: str) -> str:
    from harness.goal.draft import GoalDraftError, answer_draft, format_draft

    try:
        return format_draft(answer_draft(text))
    except GoalDraftError as exc:
        return str(exc)


def _handle_revise(target: str) -> str:
    from harness.goal.draft import GoalDraftError, create_draft, discard_draft, load_draft, format_draft

    existing = load_draft()
    if existing:
        discard_draft()
    try:
        return format_draft(create_draft(target, verification=existing.verification if existing else None, limits=existing.limits if existing else None))
    except GoalDraftError as exc:
        return str(exc)


def _handle_discard() -> str:
    from harness.goal.draft import discard_draft, load_draft

    if load_draft() is None:
        return "No Goal draft to discard."
    discard_draft()
    return "Goal draft discarded. No tests or implementation were written."


def handle_goal_draft_answer(text: str) -> str | None:
    """Consume a normal TUI message only while a draft awaits an answer."""
    from harness.goal.draft import GoalDraftError, answer_draft, format_draft, load_draft

    draft = load_draft()
    if draft is None or draft.status != "clarifying":
        return None
    try:
        return format_draft(answer_draft(text))
    except GoalDraftError as exc:
        return str(exc)


def _handle_approve(runner, history: list, context: dict, binding: Any) -> str:
    from harness.goal.models import GoalState
    from harness.goal.runner import GoalRequest
    from harness.goal.draft import GoalDraftError, draft_project_root, validate_ready_draft, mark_draft_consumed, mark_draft_start_failed
    from harness.settings import get_workdir

    try:
        draft = validate_ready_draft()
    except GoalDraftError as exc:
        return str(exc)
    request = GoalRequest(
        target=draft.target,
        verification=draft.verification,
        execution_workspace=str(draft_project_root(draft, get_workdir())),
        draft_id=draft.id,
        task_plan=draft.task_plan,
        goal_contract={
            "target": draft.target,
            "clarifications": [
                {"question": question, "answer": answer}
                for question, answer in zip(draft.questions, draft.answers)
            ],
            "autonomy": (
                "After /goal run, resolve ordinary product and technical ambiguity "
                "from this contract and repository evidence. Do not ask the user."
            ),
        },
        await_execution_approval=True,
        **draft.limits,
    )
    note = _start_precondition_note(request)
    if note is not None:
        mark_draft_start_failed(note)
        return note
    # Validate every execution role before consuming the Draft. This turns a
    # late worker/evaluator route failure into one actionable report and keeps
    # `/goal resume` meaningful after configuration is corrected.
    from harness.goal.preflight import EXECUTION_AGENT_TYPES, preflight_goal_agents

    execution_preflight = preflight_goal_agents(EXECUTION_AGENT_TYPES)
    if not execution_preflight.ok:
        note = execution_preflight.format(title="Goal execution provider preflight failed")
        mark_draft_start_failed(note)
        return note
    try:
        state = runner.start_goal(request, history=history, context=context, binding=binding)
    except BaseException as exc:
        mark_draft_start_failed(f"Goal start failed: {type(exc).__name__}: {exc}")
        raise
    mark_draft_consumed()
    assert isinstance(state, GoalState)
    return (
        f"Goal test preparation started: {state.id} [INITIALIZE]\n"
        f"  Target: {state.target}\n"
        f"  Verification: {state.verification}\n"
        "  The Goal will pause after generated tests have a failing baseline.\n"
        "  Review them, then run: /goal run\n"
        f"  Worker round limit: {state.worker_round_limit}; "
        f"operation timeout: {state.operation_timeout_seconds}s\n"
        "  Goal lifetime: unbounded (workers automatically hand off)."
    )


def _handle_run(runner, history: list, context: dict, binding: Any) -> str:
    from harness.goal.models import StopReason
    from harness.goal.store import load_goal

    state = load_goal()
    if state is None or state.stop_reason != StopReason.user_approval_required.value:
        return "No Goal is waiting for execution approval. Use /goal resume for an ordinary paused Goal."
    resumed = runner.resume_goal(
        history=history,
        context=context,
        binding=binding,
        approve_execution=True,
    )
    return f"Goal execution approved: {resumed.id} [SELECT_TASK]"
