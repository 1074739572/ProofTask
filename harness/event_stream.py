"""JSONL event-stream entrypoint for the TypeScript Ink TUI.

Protocol:
- stdin: one JSON command per line, e.g.
  {"type":"user_message","text":"...","goal_context":false}
- stdout: one JSON event per line, emitted via harness.ui.events
- stderr: diagnostics / legacy prints when unavoidable
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from harness.agent.cancel import clear_cancel, request_cancel
from harness.cli import (
    _match_cli_command,
    _resolve_open_directory,
    bootstrap_cli_session,
    handle_permission_command,
    print_turn_assistants,
)
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.loop import agent_loop, agent_lock
from harness.messages.repair import repair_tool_pairing
from harness.project.resume import checkpoint_history
from harness.project.resume import is_new_session_command
from harness.project.session_undo import abort_inflight_turn
from harness.project.session_registry import touch_session_title_from_query
from harness.prompts.lookup import is_lookup_active
from harness.prompts.writing import is_writing_query
from harness.rag.bootstrap import bootstrap_message, ensure_rag_indexed
from harness.todos.state import get_todos
from harness.ui import events
from harness.ui.events import emit, suppress_events


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


#: Slash commands that are NON-interactive control commands: they take effect
#: instantly (no LLM round, no context/history/binding mutation) and therefore
#: must be executable while the agent is running — never flip the UI into the
#: "running" state, never print "Agent is already running".
_INSTANT_SLASH_PREFIXES = (
    "/model",
    "/effort",
    "/mode",
    "/models",
    "/usage",
    "/help",
    "/permission",
)
MAX_PENDING_TURNS = 32
_ACTIVE_GOAL_DRAFT_STAGES = frozenset({"preflight", "catalog", "intake", "discovering", "planning"})


def _is_goal_control_command(query: str) -> bool:
    """True for /goal status|pause|stop|cancel — instant while the goal runs.

    /goal start/resume require the normal turn queue (no ordinary turn may be
    running), so they are deliberately NOT classified as instant here.
    """
    try:
        from harness.goal.commands import parse_goal_subcommand
    except ImportError:
        return False
    return parse_goal_subcommand(query) in ("status", "pause", "cancel")


def _is_goal_background_command(query: str, *, goal_context: bool = False) -> bool:
    """Commands whose Draft/Goal setup may perform slow external work."""
    if _is_goal_draft_answer(query, goal_context=goal_context):
        return True
    try:
        from harness.goal.commands import parse_goal_subcommand

        return parse_goal_subcommand(query) in {"draft", "answer", "approve", "run", "resume", "revise"}
    except ImportError:
        return False


def _is_goal_draft_answer(query: str, *, goal_context: bool = False) -> bool:
    """Whether a message explicitly submitted from the Draft view is an answer."""
    if not goal_context or not query.strip() or query.lstrip().startswith("/"):
        return False
    try:
        from harness.goal.draft import load_draft

        draft = load_draft()
        return bool(draft and draft.status == "clarifying" and draft.unanswered_question)
    except ValueError:
        return False


def _active_goal_draft_stage() -> str | None:
    """Return the durable Draft stage for progress diagnostics, if any."""
    try:
        from harness.goal.draft import load_draft

        draft = load_draft()
    except (OSError, ValueError):
        return None
    if draft is None or draft.status in {"paused", "cancelled", "ready", "approved", "failed", "consumed"}:
        return None
    return draft.stage if draft.stage in _ACTIVE_GOAL_DRAFT_STAGES else None


def _is_instant_slash_command(query: str) -> bool:
    """True when `query` is an instant configuration switch (model/mode/effort).

    These are pure configuration changes (or read-only listings): they do not
    need the agent, do not mutate context/history/binding, and are safe to run
    concurrently with a running turn. Everything else (messages, /open, /resume,
    /clear, /rag) goes through the normal turn queue.
    """
    q = query.strip().lower()
    if not q:
        return False
    return any(q == p or q.startswith(p + " ") for p in _INSTANT_SLASH_PREFIXES)


def _status_payload(context: dict, binding, history: list) -> dict:
    from harness.agent.compact.sizing import estimate_tokens, model_context_window
    from harness.models import get_model, get_reasoning_effort, list_efforts
    from harness.modes import get_mode
    from harness.permission_session import get_permission_mode
    from harness.settings import get_workdir
    from harness.usage.context import current_context_tokens, scaled_context_breakdown
    from harness.usage.store import totals_for_day

    usage = totals_for_day()
    effort_items = list_efforts()
    effort_id = get_reasoning_effort() or "off"
    effort_label = next((item.get("label") for item in effort_items if item.get("current")), "Model default")
    session_id = str(getattr(binding, "session_id", "") or "")
    # The API prompt count includes system instructions and tool schemas,
    # which history-only estimation cannot see. Use it once available.
    ctx_tokens = current_context_tokens(session_id, estimate_tokens(history or []))
    ctx_breakdown = scaled_context_breakdown(session_id, ctx_tokens)
    return {
        "model": get_model(),
        "mode": get_mode(),
        "permission_mode": get_permission_mode(),
        "reasoning_effort": effort_id,
        "reasoning_effort_label": effort_label,
        "reasoning_effort_options": effort_items,
        "cwd": str(get_workdir()),
        "session_id": getattr(binding, "session_id", ""),
        "running": False,
        "session_source": context.get("session_source", ""),
        "today_input_tokens": usage.input_tokens,
        "today_output_tokens": usage.out,
        "today_cache_read_tokens": usage.hit,
        "today_cache_hit_rate": usage.hit_rate,
        "ctx_tokens": ctx_tokens,
        "ctx_window": model_context_window(),
        "ctx_system": ctx_breakdown["system"],
        "ctx_tools": ctx_breakdown["tools"],
        "ctx_messages": ctx_breakdown["messages"],
    }


def _emit_welcome() -> None:
    """Mirror the CLI startup welcome (smiley art + today's quote) as one event."""
    from harness.ui.banner import SMILEY
    from harness.ui.quotes import get_daily_quote

    emit(
        "welcome",
        art=list(SMILEY),
        quote=get_daily_quote(),
        date=date.today().isoformat(),
    )


def _emit_history_replay(
    history: list,
    *,
    limit: int = 300,
    replace: bool = False,
    session_id: str | None = None,
    new_session: bool = False,
) -> None:
    """Send a bounded, display-ready transcript to the TUI.

    Startup replay is additive only when the viewport is still empty.  Session
    switches and ``/new`` pass ``replace=True`` so an already-rendered
    transcript is replaced (including with an empty list for a brand-new
    session) instead of leaving the previous chat on screen.
    """
    from harness.project.resume import _message_text

    rows = []
    for index, message in enumerate(history[-max(1, limit):]):
        role = str(message.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message.get("content")).strip()
        if not text:
            continue
        rows.append({"id": f"history-{index}", "role": role, "text": text})
    if rows or replace:
        payload: dict[str, Any] = {
            "messages": rows,
            "truncated": len(history) > limit,
        }
        if replace:
            payload["replace"] = True
        if new_session:
            payload["new_session"] = True
        if session_id:
            payload["session_id"] = str(session_id)
        emit("history_replay", **payload)


def _emit_status(context: dict, binding, history: list, *, running: bool = False) -> None:
    payload = _status_payload(context, binding, history)
    payload["running"] = running
    emit("session_status", **payload)
    emit("task_update", tasks=get_todos())


def _handle_open_workspace(query: str) -> tuple[str | None, str | None, bool]:
    """Resolve a TUI `/open` request.

    Returns ``(note, target_cwd, list_mode)``.

    - ``list_mode=True``: the user typed bare ``/open`` — the caller should emit
      the recent-projects list (``workspace_list`` event) instead of switching.
    - otherwise ``target_cwd`` is the resolved workspace directory to switch to
      (in-process, no restart), or ``note`` holds the error message.
    """
    raw_path = query.strip()[len("/open") :].strip()
    if not raw_path:
        return None, None, True

    # `/open <N>` selects the Nth recently opened project.
    if raw_path.isdigit():
        from harness.workspace import list_recent_projects

        projects = list_recent_projects()
        index = int(raw_path)
        if index < 1 or index > len(projects):
            return f"序号超出范围（1–{len(projects)}）", None, False
        target = projects[index - 1]["path"]
        return f"Switching workspace to {target}", target, False

    workspace, error = _resolve_open_directory(query)
    if workspace is None:
        return error, None, False
    return f"Switching workspace to {workspace}", str(workspace), False


def _emit_workspace_list() -> None:
    """Emit the recent-projects list as a structured event + readable log."""
    from harness.workspace import list_recent_projects

    projects = list_recent_projects()
    emit("workspace_list", projects=projects)
    if not projects:
        emit("log", level="plain", text="还没有打开过其他项目。用法：/open <目录>")
        return
    lines = ["最近打开的项目：/open <目录> 切换（或 /open 序号）"]
    for index, project in enumerate(projects, start=1):
        mark = "  ← 当前" if project["current"] else ""
        lines.append(f"  {index}. {project['path']}{mark}")
    emit("log", level="plain", text="\n".join(lines))


_SLASH_COMPLETIONS = (
    "/clear",
    "/effort",
    "/goal",
    "/help",
    "/init",
    "/mode",
    "/model",
    "/models",
    "/new",
    "/open",
    "/permission",
    "/rag",
    "/resume",
    "/usage",
)


def _handle_completion_request(command: dict) -> dict:
    """Resolve a TUI autocomplete request into paths or slash commands.

    ``@path`` delegates filesystem lookup to the shared CLI completion core,
    but returns only the replacement *path* rather than a completed copy of the
    entire composer line.  A leading slash command uses the token before the
    cursor and never activates for slash text in ordinary prose.
    """
    from harness.path_completion import complete_paths, path_completion_context
    from harness.settings import get_workdir

    text = str(command.get("text") or "")
    cursor = command.get("cursor")
    try:
        cursor_pos = int(cursor) if cursor is not None else len(text)
    except (TypeError, ValueError):
        cursor_pos = len(text)
    cursor_pos = max(0, min(cursor_pos, len(text)))
    before_cursor = text[:cursor_pos]

    candidates: list[str]
    # Dynamic slash completion applies only to the first token at composer
    # start. For example, "/mo keep" with cursor after /mo still suggests
    # /model and /mode; "please /mo" never does.
    if before_cursor.startswith("/") and not any(char.isspace() for char in before_cursor):
        token = before_cursor.split(maxsplit=1)[0]
        candidates = [item for item in _SLASH_COMPLETIONS if item.startswith(token.lower())]
    else:
        context = path_completion_context(text, cursor_pos)
        if context is None:
            candidates = []
        else:
            suffix = text[context.end :]
            completed = complete_paths(text, cursor_pos, cwd=get_workdir())
            # ``complete_paths`` serves readline and returns full lines. The
            # TUI menu instead needs the selected path alone.
            candidates = [
                item[context.start : len(item) - len(suffix) if suffix else len(item)]
                for item in completed
            ]
    return {
        "request_id": str(command.get("request_id") or ""),
        "candidates": candidates,
    }


def _handle_slash_command(query: str, history: list, binding) -> tuple[str | None, object]:
    """Handle a small safe subset of slash commands for the TUI bridge.

    Returns (note, binding) — binding may be a new session binding after /resume.
    """
    from harness.models import handle_model_command, handle_effort_command, list_efforts
    from harness.modes import set_mode, format_mode_catalog
    from harness.rag.commands import run_rag_cli_command
    from harness.usage import handle_usage_command
    from harness.project.resume import is_new_session_command, new_session_title, start_new_session

    # ``new`` and ``/new`` are aliases for an explicit fresh chat.  Keep this
    # before the other slash handlers so it never becomes an LLM prompt.
    if is_new_session_command(query):
        from harness.goal.runner import is_goal_running

        if is_goal_running():
            return "Cannot /new while a goal is running — use /goal pause or /goal cancel first.", binding
        new_binding, note = start_new_session(
            history,
            binding=binding,
            title=new_session_title(query),
        )
        # The caller owns the event-stream history reference; publish a full
        # replacement even though the new session normally starts empty.
        _emit_history_replay(
            history,
            replace=True,
            new_session=True,
            session_id=new_binding.session_id,
        )
        return note, new_binding

    if _match_cli_command(query, "/model"):
        parts = query.strip().split(maxsplit=1)
        sub = parts[1] if len(parts) > 1 else ""
        if not sub:
            from harness.models import list_models
            models = list_models()
            if models and len(models) > 1:
                emit("show_picker", id="model", title="Select model", items=[
                    {"id": m["id"], "label": m.get("label", m["id"]), "detail": m.get("provider", "")}
                    for m in models
                ])
                return "", binding
        return handle_model_command(query), binding
    if _match_cli_command(query, "/effort"):
        parts = query.strip().split(maxsplit=1)
        sub = parts[1] if len(parts) > 1 else ""
        if not sub:
            emit("show_picker", id="effort", title="Select reasoning effort", items=[
                {"id": e["id"], "label": e["label"], "detail": e.get("detail", "")}
                for e in list_efforts()
            ])
            return "", binding
        return handle_effort_command(query), binding
    if _match_cli_command(query, "/mode"):
        from harness.modes import format_mode_catalog, get_mode_profile, list_mode_ids, set_mode
        parts = query.strip().split(maxsplit=1)
        if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker", "help"):
            modes = [profile for mid in list_mode_ids() if (profile := get_mode_profile(mid)) is not None]
            if modes and len(modes) > 1:
                emit("show_picker", id="mode", title="Select mode", items=[
                    {"id": m.id, "label": m.label or m.id, "detail": m.summary or ""}
                    for m in modes
                ])
                return "", binding
            return format_mode_catalog(), binding
        return set_mode(parts[1]), binding
    if _match_cli_command(query, "/usage"):
        return handle_usage_command(query), binding
    if _match_cli_command(query, "/permission"):
        parts = query.strip().split(maxsplit=1)
        if len(parts) == 1:
            from harness.permission_session import PERMISSION_MODES, get_permission_mode
            labels = {
                "default": "默认权限（低风险自动放行）",
                "auto-review": "自动审查（低、中风险自动放行）",
                "full-access": "完全访问（低、中、高风险自动放行）",
            }
            current = get_permission_mode()
            emit(
                "show_picker",
                id="permission",
                title="Select permission mode",
                items=[
                    {"id": mode, "label": f"{labels[mode]} [{mode}]" + (" · 当前" if mode == current else ""), "detail": "Session-only"}
                    for mode in PERMISSION_MODES
                ],
            )
            return "", binding
        return handle_permission_command(query), binding
    if _match_cli_command(query, "/rag"):
        return run_rag_cli_command(query), binding
    if _match_cli_command(query, "/clear"):
        from harness.goal.runner import is_goal_running

        if is_goal_running():
            return "Cannot /clear while a goal is running — use /goal pause or /goal cancel first.", binding
        # Match classic CLI semantics: archive the current conversation,
        # create a new binding, and notify the TUI to replace its transcript.
        from harness.project.resume import start_new_session

        new_binding, note = start_new_session(history, binding=binding)
        _emit_history_replay(
            history,
            replace=True,
            new_session=True,
            session_id=getattr(new_binding, "session_id", None),
        )
        return note, new_binding
    if _match_cli_command(query, "/resume"):
        from harness.goal.runner import is_goal_running

        if is_goal_running():
            return "Cannot /resume while a goal is running — use /goal pause or /goal cancel first.", binding
        from harness.project.session_registry import visible_session_summaries
        from harness.project.resume import run_resume_command

        parts = query.strip().split(maxsplit=1)
        sub = parts[1] if len(parts) > 1 else ""
        if sub:
            note, new_binding = run_resume_command(sub, messages=history, binding=binding)
            previous_id = getattr(binding, "session_id", None)
            new_id = getattr(new_binding, "session_id", None)
            if new_binding is not None and new_binding is not binding and new_id != previous_id:
                from harness.permission_session import reset_permission_session

                reset_permission_session()
            selected = new_binding or binding
            if selected is not binding:
                _emit_history_replay(history, replace=True, session_id=getattr(selected, "session_id", None))
            return note, selected
        sessions = visible_session_summaries(limit=20)
        if not sessions:
            return "No saved sessions.", binding
        import time as _time
        if len(sessions) > 1:
            items = []
            for idx, s in enumerate(sessions):
                ts = _time.strftime("%m/%d %H:%M", _time.localtime(s.get("created_at") or 0))
                title = (s.get("title") or "(untitled)")[:40]
                items.append({"id": str(idx + 1), "label": title, "detail": ts})
            emit("show_picker", id="resume", title="Resume session", items=items)
            return "", binding
        # Single session: just resume it
        note, new_binding = run_resume_command("1", messages=history, binding=binding)
        selected = new_binding or binding
        if selected is not binding:
            _emit_history_replay(history, replace=True, session_id=getattr(selected, "session_id", None))
        return note, selected
    if _match_cli_command(query, "/models"):
        from harness.models import list_models
        models = list_models()
        lines = ["Models:"]
        for m in models:
            lines.append(f"  {m['id']} — {m['label']}")
        return "\n".join(lines), binding
    if query.strip().lower() in ("/help",):
        return (
            "TUI commands:\n"
            "  /open <directory>  — switch workspace (instant, no restart)\n"
            "  /goal <target>  — clarify and preview a verified Goal\n"
            "  /goal approve  — write approved tests and start execution\n"
            "  /goal status|pause|stop|resume|cancel  — control the goal\n"
            "  /init  — scan repo & create/improve HARNESS.md handbook\n"
            "  @path + Tab        — complete file/dir path\n"
            "  /model <id>  — switch model (use /models to list)\n"
            "  /effort      — choose reasoning effort\n"
            "  /permission [mode] — view/set session permissions\n"
            "  /models      — list available models\n"
            "  /mode <id>   — switch mode\n"
            "  /mode        — list modes\n"
            "  /resume      — list saved sessions\n"
            "  /resume <N>  — switch to session N\n"
            "  new [title]   — start a fresh chat (old history stays resumable)\n"
            "  /new [title]  — same as new\n"
            "  /clear       — clear session\n"
            "  Ctrl+C       — interrupt agent\n"
            "  Ctrl+L       — clear display\n"
            "  Ctrl+Q       — quit"
        ), binding
    if query.strip().lower() == "/resume":
        return _handle_slash_command(query, history, binding)  # handled above, just in case
    return None, binding


def _run_instant_slash_command(text: str, context: dict, history: list, binding, *, running: bool) -> None:
    """Run an instant (non-interactive) slash command, then push a status refresh.

    Model/mode/effort switches change runtime config but only produce a log
    note; the extra ``session_status`` makes the TUI's model/mode label refresh
    immediately instead of waiting for the next real turn. ``running`` must be
    the true agent state so a busy agent doesn't look idle.
    """
    note, _new_binding = _handle_slash_command(text, history, binding)
    if note:
        emit("log", level="plain", text=note)
    _emit_status(context, binding, history, running=running)


def _run_user_turn(
    query: str,
    history: list,
    context: dict,
    binding,
    *,
    echo_user: bool = True,
    goal_context: bool = False,
) -> tuple[dict, bool, object]:
    """Run one user turn. Returns (possibly updated context, interrupted, binding)."""
    # Slash commands are internal instructions: never echo them as transcript
    # messages, regardless of echo_user. Feedback is delivered via log events.
    if echo_user and not query.strip().startswith("/") and not is_new_session_command(query):
        emit("user_message", text=query, silent=False)

    if _is_goal_draft_answer(query, goal_context=goal_context):
        from harness.goal.commands import handle_goal_draft_answer

        note = handle_goal_draft_answer(query)
        if note:
            emit("assistant_message", text=note)
            return context, False, binding

    # /open switches the workspace in-process (no backend restart): validate,
    # flip the active workspace root, re-bind sessions, and reset RAG caches.
    # Emits `workspace_switched` so the TUI can refresh the header; the process
    # keeps running with zero SDK re-imports.
    if _match_cli_command(query, "/open"):
        from harness.goal.runner import is_goal_running

        if is_goal_running():
            emit(
                "log",
                level="warn",
                text="Cannot /open while a goal is running — use /goal pause or /goal cancel first.",
            )
            return context, False, binding
        note, target, list_mode = _handle_open_workspace(query)
        if list_mode:
            _emit_workspace_list()
            return context, False, binding
        if target is None:
            emit("log", level="warn", text=note)
            return context, False, binding
        from harness.workspace import switch_workspace

        ok, result, new_binding = switch_workspace(target)
        if not ok:
            emit("log", level="warn", text=result)
            return context, False, binding
        if new_binding is not None:
            from harness.todos.state import set_binding as _todos_set_binding

            _todos_set_binding(new_binding)
            binding = new_binding
        emit("workspace_switched", cwd=result.replace("Switched workspace → ", "").strip())
        emit("log", level="plain", text=result)
        # Start a fresh session in the new workspace: drop the old project's
        # history AND reload the new project's HARNESS.md. Otherwise the agent
        # keeps following the previous project's rules (old context leak).
        from harness.prompts.project_md import apply_project_instructions
        from harness.prompts.ephemeral import reset_ephemeral_cache
        from harness.context import update_context as _update_context

        history.clear()
        context = _update_context({}, [])
        apply_project_instructions(context, start=Path(target))
        reset_ephemeral_cache()
        from harness.permission_session import reset_permission_session

        reset_permission_session()
        return context, False, binding

    if query.strip().startswith("/") or is_new_session_command(query):
        if _match_cli_command(query, "/goal"):
            from harness.goal.commands import handle_goal_command

            command_note = handle_goal_command(query, history, context, binding)
            new_binding = binding
        elif _match_cli_command(query, "/init"):
            from harness.goal.runner import is_goal_running

            if is_goal_running():
                command_note = (
                    "Cannot /init while a goal is running — "
                    "use /goal pause or /goal cancel first."
                )
            else:
                from harness.prompts.init_md import handle_init_command

                emit("log", level="plain", text="Scanning repository and writing HARNESS.md …")
                try:
                    command_note = handle_init_command()
                except Exception as exc:
                    command_note = f"/init failed: {exc}"
            new_binding = binding
        else:
            command_note, new_binding = _handle_slash_command(query, history, binding)
    else:
        command_note, new_binding = None, binding
    if command_note is not None:
        if command_note:
            emit("log", level="plain", text=command_note)
        if new_binding is not binding:
            from harness.todos.state import set_binding as _todos_set_binding
            from harness.prompts.project_md import apply_project_instructions

            _todos_set_binding(new_binding)
            # A session switch/new chat must not inherit turn-scoped context
            # (writing/RAG constraints, stale session source, etc.). Rebuild
            # the context from the newly loaded history and project rules.
            fresh_context = update_context({}, history)
            apply_project_instructions(fresh_context)
            return fresh_context, False, new_binding
        return update_context(context, history), False, new_binding

    from harness.modes import get_mode, note_user_query_for_mode
    from harness.rag.file_mode import handle_file_mode_turn, is_file_mode

    from harness.goal.runner import is_goal_running

    if is_goal_running():
        emit("log", level="warn", text="Goal is running. Use /goal status|pause|cancel.")
        return context, False, binding

    if is_file_mode() or get_mode() == "file":
        answer = handle_file_mode_turn(query)
        emit("assistant_message", text=answer)
        return context, False, binding

    gate_note = note_user_query_for_mode(query)
    if gate_note:
        emit("log", level="muted", text=gate_note)

    trigger_hooks("UserPromptSubmit", query)
    # The TUI submits a lightweight @path reference. Expand it only for the
    # model-facing message; transcript/UI/history metadata retain the user's
    # original wording. Failed or out-of-workspace references stay literal and
    # are recorded as a muted note instead of breaking the turn.
    from harness.mentions import expand_mentions
    from harness.settings import get_workdir

    model_query, mention_notes = expand_mentions(query, base_dir=get_workdir())
    for note in mention_notes:
        if not note.ok:
            emit("log", level="warn", text=f"@引用未展开: {note.path} ({note.error or 'unknown error'})")

    touch_session_title_from_query(query, binding=binding)
    repair_tool_pairing(history)
    turn_start = len(history)
    history.append({"role": "user", "content": model_query})
    lookup_active = is_lookup_active(query)
    context["lookup_active"] = lookup_active
    context["writing_mode"] = is_writing_query(query) and not lookup_active
    from harness.prompts.goal_stickiness import augment_if_needed
    from harness.agent.question_state import pending_question_text
    from harness.prompts.lookup import LOOKUP_CONSTRAINT
    from harness.prompts.writing import WRITING_CONSTRAINT

    constraints = []
    sticky = augment_if_needed(query, has_pending_question=bool(pending_question_text(context)))
    if sticky:
        constraints.append(sticky[len(query):].strip())
    if lookup_active:
        constraints.append(LOOKUP_CONSTRAINT.strip())
    elif context["writing_mode"]:
        constraints.append(WRITING_CONSTRAINT.strip())
    context["turn_constraints"] = "\n\n".join(item for item in constraints if item)
    context.pop("rag_bootstrap", None)
    if context["writing_mode"]:
        boot = ensure_rag_indexed("files")
        context["rag_bootstrap"] = bootstrap_message(boot)
        emit(
            "log",
            level="muted" if boot.get("ok") else "warn",
            text=(context["rag_bootstrap"].split("\n")[0] if boot.get("ok") else context["rag_bootstrap"][:200]),
        )

    interrupted = False
    clear_cancel()
    _emit_status(context, binding, history, running=True)
    try:
        with agent_lock:
            try:
                interrupted = agent_loop(history, context, turn_start=turn_start, binding=binding)
            except KeyboardInterrupt:
                request_cancel()
                interrupted = True
    finally:
        clear_cancel()
        _emit_status(context, binding, history, running=False)

    if not interrupted:
        from harness.agent.question_state import remember_turn_question

        remember_turn_question(context, history, turn_start)

    if interrupted:
        message, _rolled_back = abort_inflight_turn(history, turn_start, binding=binding)
        emit("log", level="warn", text=message)
        context = update_context(context, history)
    else:
        with agent_lock:
            context = update_context(context, history)
        # Classic final answer path may still be the safety net. It emits events,
        # but suppress human stdout in JSONL mode by redirecting in run_event_stream.
        print_turn_assistants(history, turn_start)
        checkpoint_history(history, binding=binding)

    return context, interrupted, binding


def _startup_session_picker_items(*, limit: int = 20) -> list[dict[str, str]]:
    """Build startup rows from the same visible sessions as ``/resume``."""
    from harness.project.session_registry import visible_session_summaries

    items: list[dict[str, str]] = []
    for summary in visible_session_summaries(limit=limit):
        title_value = str(summary.get("title") or "(untitled)")
        if int(summary.get("messages") or 0) <= 0 and title_value in {"(untitled)", "(migrated)", ""}:
            continue
        timestamp = int(summary.get("updated_at") or summary.get("created_at") or 0)
        detail = time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp)) if timestamp else "—"
        items.append({"id": str(summary.get("id") or ""), "label": title_value[:60], "detail": detail})
    return items or [{"id": "__empty__", "label": "暂无历史会话", "detail": "输入消息开始新会话"}]


def run_event_stream() -> None:
    # Redirect ALL stdout to stderr at the very start, before any imports
    # that may print (MCP servers, etc.). JSON events use events.emit()
    # which writes to a dedicated sink, not sys.stdout.
    sys.stdout = sys.stderr

    # Keep classic renderer output off stdout; JSON events own stdout.
    events.enable_event_stream(sys.__stdout__)
    history, context, binding = bootstrap_cli_session(
        welcome=False,
        start_cron=False,
        cli_active=False,
    )
    state_lock = threading.Lock()
    turn_queue: "queue.Queue[tuple[str, bool, bool] | None]" = queue.Queue(maxsize=MAX_PENDING_TURNS)
    running = threading.Event()
    goal_turn_active = threading.Event()
    shutdown = threading.Event()

    def emit_queue_status() -> None:
        emit("queue_status", pending=turn_queue.qsize(), running=running.is_set(), capacity=MAX_PENDING_TURNS)

    def worker() -> None:
        nonlocal context, binding
        while not shutdown.is_set():
            item = turn_queue.get()
            if item is None:
                turn_queue.task_done()
                break
            query, echo_user, goal_context = item
            # Most slash commands are instant, but Draft/Goal setup can spend
            # minutes in catalog/discovery/planning. Treat those as a real
            # turn so the TUI gets an active phase and heartbeat events.
            is_background = _is_goal_background_command(query, goal_context=goal_context)
            is_slash = (
                query.strip().startswith("/")
                or is_new_session_command(query)
                or _is_goal_draft_answer(query, goal_context=goal_context)
            )
            if not is_slash or is_background:
                running.set()
                emit("agent_start", phase="goal_draft" if is_background else "preparing")
            emit_queue_status()
            _interrupted = False
            try:
                with state_lock:
                    context, _interrupted, binding = _run_user_turn(
                        query,
                        history,
                        context,
                        binding,
                        echo_user=echo_user,
                        goal_context=goal_context,
                    )
                    _emit_status(context, binding, history, running=False)
            except Exception as exc:
                emit("error", text=f"Turn failed: {exc}")
            finally:
                clear_cancel()
                if not is_slash or is_background:
                    running.clear()
                    emit("agent_end", status="interrupted" if _interrupted else "done")
                if is_background:
                    goal_turn_active.clear()
                emit_queue_status()
                turn_queue.task_done()

    worker_thread = threading.Thread(target=worker, name="harness-event-stream-worker", daemon=True)
    worker_thread.start()

    # Replay the conversation before the status/task snapshot.  The latter
    # may contain an empty task update, and treating that UI-only row as chat
    # content would make the TUI incorrectly skip the history replay.
    _emit_history_replay(history, session_id=getattr(binding, "session_id", None))
    _emit_status(context, binding, history, running=False)
    # On launch, reuse the /resume picker protocol so users can immediately
    # choose a persisted conversation without typing a command first.
    from harness.project.session_registry import visible_session_summaries

    session_items = _startup_session_picker_items(limit=20)
    emit("show_picker", id="startup_history", title="历史会话", items=session_items, startup=True)
    # Restore a paused/in-progress Goal after a TUI restart. Terminal Goals stay
    # out of the startup view, but `/goal status` can still request them.
    from harness.goal.runner import emit_current_goal_status

    emit_current_goal_status(include_terminal=False, hydrated=True)
    try:
        from harness.goal.draft import emit_current_draft_status

        emit_current_draft_status()
    except Exception:
        pass
    _emit_welcome()
    emit("ready")
    emit_queue_status()

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                command = json.loads(raw)
            except json.JSONDecodeError as exc:
                emit("error", text=f"Invalid JSON command: {exc}")
                continue
            ctype = command.get("type")
            if ctype in ("exit", "quit"):
                shutdown.set()
                request_cancel()
                emit("exit")
                break
            if ctype == "permission_response":
                from harness.ui.permission_events import reply_permission
                ok = reply_permission(str(command.get("id") or ""), str(command.get("decision") or "deny"))
                if not ok:
                    emit("log", level="warn", text="Permission response did not match an active request")
                continue
            if ctype == "interrupt":
                request_cancel()
                emit("log", level="warn", text="Interrupt requested")
                continue
            if ctype == "clear":
                emit("ui_clear")
                continue
            if ctype == "completion_request":
                payload = _handle_completion_request(command)
                emit("completion_result", **payload)
                continue
            if ctype in ("user_message", "slash_command"):
                text = str(command.get("text") or command.get("command") or "").strip()
                if not text:
                    continue
                # Non-interactive control commands (model/mode/effort switches
                # and /goal status|pause|stop|cancel) run instantly — even while the
                # agent is busy. They need no LLM round and never flip the UI
                # into the "running" state.
                if _is_goal_control_command(text):
                    from harness.goal.commands import handle_goal_command, parse_goal_subcommand

                    note = handle_goal_command(text, history, context, binding)
                    subcommand = parse_goal_subcommand(text)
                    if subcommand == "resume":
                        from harness.goal.runner import emit_current_goal_status

                        # Resume may reach a durable pause checkpoint before
                        # this control command returns; publish that final
                        # snapshot so the TUI cannot remain stuck on "working".
                        emit_current_goal_status(include_terminal=True)
                    elif subcommand == "status":
                        from harness.goal.draft import emit_current_draft_status

                        draft = emit_current_draft_status(event="status")
                        if draft is None or draft.status == "consumed":
                            from harness.goal.runner import emit_current_goal_status

                            emit_current_goal_status(include_terminal=True)
                    if note:
                        emit("log", level="plain", text=note)
                    continue
                if _is_instant_slash_command(text):
                    # Model/mode/effort switches change runtime config but only
                    # emit a log note — push a fresh session_status so the TUI's
                    # model/mode label refreshes immediately instead of on the
                    # next real turn. Keep the true running flag so a busy agent
                    # doesn't look idle.
                    _run_instant_slash_command(text, context, history, binding, running=running.is_set())
                    continue
                # Draft setup is a foreground transaction even though its
                # discovery/model calls can be slow. Never queue another turn
                # behind it: by the time that message ran, its context and the
                # user's intended meaning could have changed completely.
                if goal_turn_active.is_set():
                    stage = _active_goal_draft_stage() or "starting"
                    emit(
                        "log",
                        level="warn",
                        text=(
                            f"Goal draft is {stage}; new messages are not queued. "
                            "Use /goal status, /goal pause, or /goal cancel."
                        ),
                    )
                    continue
                # An autonomous Goal owns the workspace and its context. Do
                # not enqueue ordinary chat into a queue that would later run
                # against a different contract; the user can use Goal
                # controls instantly and resume normal chat after it pauses.
                try:
                    from harness.goal.runner import is_goal_running

                    if is_goal_running():
                        emit("log", level="warn", text="Goal is running; use /goal status, /goal pause, or /goal cancel.")
                        continue
                except Exception:
                    pass
                silent = bool(command.get("silent"))
                goal_context = command.get("goal_context") is True
                is_goal_background = _is_goal_background_command(text, goal_context=goal_context)
                if is_goal_background:
                    # Set ownership before publishing to the worker. A fast
                    # failure could otherwise clear the flag before this
                    # thread set it, leaving Draft input locked forever.
                    goal_turn_active.set()
                try:
                    turn_queue.put_nowait((text, not silent, goal_context))
                    pending = turn_queue.qsize()
                    if running.is_set() or pending > 1:
                        emit("message_queued", text=text, position=pending, pending=pending)
                    emit_queue_status()
                except queue.Full:
                    if is_goal_background:
                        goal_turn_active.clear()
                    emit("log", level="warn", text=f"Message queue is full ({MAX_PENDING_TURNS}). Wait for the current turn to finish.")
                continue
            emit("error", text=f"Unknown command type: {ctype}")
    finally:
        shutdown.set()
        try:
            turn_queue.put_nowait(None)
        except queue.Full:
            pass
        worker_thread.join(timeout=1.0)


if __name__ == "__main__":
    run_event_stream()
