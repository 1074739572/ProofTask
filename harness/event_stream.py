"""JSONL event-stream entrypoint for the TypeScript Ink TUI.

Protocol:
- stdin: one JSON command per line, e.g. {"type":"user_message","text":"..."}
- stdout: one JSON event per line, emitted via harness.ui.events
- stderr: diagnostics / legacy prints when unavoidable
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Any

from harness.agent.cancel import clear_cancel, request_cancel
from harness.cli import (
    _match_cli_command,
    bootstrap_cli_session,
    print_turn_assistants,
)
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.loop import agent_loop, agent_lock
from harness.messages.repair import repair_tool_pairing
from harness.project.resume import checkpoint_history
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


def _status_payload(context: dict, binding, history: list) -> dict:
    from harness.agent.compact.sizing import estimate_tokens, model_context_window
    from harness.models import get_model
    from harness.modes import get_mode
    from harness.settings import WORKDIR
    from harness.usage.store import totals_for_day

    usage = totals_for_day()
    return {
        "model": get_model(),
        "mode": get_mode(),
        "cwd": str(WORKDIR),
        "session_id": getattr(binding, "session_id", ""),
        "running": False,
        "session_source": context.get("session_source", ""),
        "today_input_tokens": usage.input_tokens,
        "today_output_tokens": usage.out,
        "today_cache_read_tokens": usage.hit,
        "today_cache_hit_rate": usage.hit_rate,
        # Context usage for the header progress bar: rough estimate of the
        # current transcript (chars / 4) against the model's context window.
        "ctx_tokens": estimate_tokens(history or []),
        "ctx_window": model_context_window(),
    }


def _emit_status(context: dict, binding, history: list, *, running: bool = False) -> None:
    payload = _status_payload(context, binding, history)
    payload["running"] = running
    emit("session_status", **payload)
    emit("task_update", tasks=get_todos())


def _handle_slash_command(query: str, history: list, binding) -> tuple[str | None, object]:
    """Handle a small safe subset of slash commands for the TUI bridge.

    Returns (note, binding) — binding may be a new session binding after /resume.
    """
    from harness.models import handle_model_command, handle_effort_command, list_efforts
    from harness.modes import set_mode, format_mode_catalog
    from harness.project.tools import run_project_clear
    from harness.rag.commands import run_rag_cli_command
    from harness.usage import handle_usage_command

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
    if _match_cli_command(query, "/rag"):
        return run_rag_cli_command(query), binding
    if _match_cli_command(query, "/clear"):
        return run_project_clear(clear_project=False), binding
    if _match_cli_command(query, "/resume"):
        from harness.project.session_registry import visible_session_summaries
        from harness.project.resume import run_resume_command

        parts = query.strip().split(maxsplit=1)
        sub = parts[1] if len(parts) > 1 else ""
        if sub:
            note, new_binding = run_resume_command(sub, messages=history, binding=binding)
            return note, new_binding or binding
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
        return note, new_binding or binding
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
            "  /model <id>  — switch model (use /models to list)\n"
            "  /effort      — choose reasoning effort\n"
            "  /models      — list available models\n"
            "  /mode <id>   — switch mode\n"
            "  /mode        — list modes\n"
            "  /resume      — list saved sessions\n"
            "  /resume <N>  — switch to session N\n"
            "  /clear       — clear session\n"
            "  Ctrl+C       — interrupt agent\n"
            "  Ctrl+L       — clear display\n"
            "  Ctrl+Q       — quit"
        ), binding
    if query.strip().lower() == "/resume":
        return _handle_slash_command(query, history, binding)  # handled above, just in case
    return None, binding


def _run_user_turn(query: str, history: list, context: dict, binding, *, echo_user: bool = True) -> tuple[dict, bool, object]:
    """Run one user turn. Returns (possibly updated context, interrupted, binding)."""
    # Slash commands are internal instructions: never echo them as transcript
    # messages, regardless of echo_user. Feedback is delivered via log events.
    if echo_user and not query.strip().startswith("/"):
        emit("user_message", text=query, silent=False)

    if query.strip().startswith("/"):
        command_note, new_binding = _handle_slash_command(query, history, binding)
    else:
        command_note, new_binding = None, binding
    if command_note is not None:
        if command_note:
            emit("log", level="plain", text=command_note)
        if new_binding is not binding:
            from harness.todos.state import set_binding as _todos_set_binding
            _todos_set_binding(new_binding)
        return update_context(context, history), False, new_binding

    from harness.modes import get_mode, note_user_query_for_mode
    from harness.rag.file_mode import handle_file_mode_turn, is_file_mode

    if is_file_mode() or get_mode() == "file":
        answer = handle_file_mode_turn(query)
        emit("assistant_message", text=answer)
        return context, False, binding

    gate_note = note_user_query_for_mode(query)
    if gate_note:
        emit("log", level="muted", text=gate_note)

    hook_result = trigger_hooks("UserPromptSubmit", query)
    model_query = hook_result if isinstance(hook_result, str) else query

    touch_session_title_from_query(query, binding=binding)
    repair_tool_pairing(history)
    turn_start = len(history)
    history.append({"role": "user", "content": model_query})
    context["latest_user_query"] = query
    context["lookup_mode"] = is_lookup_active(query)
    context["writing_mode"] = is_writing_query(query) and not context["lookup_mode"]
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
    turn_queue: "queue.Queue[tuple[str, bool] | None]" = queue.Queue(maxsize=1)
    running = threading.Event()
    shutdown = threading.Event()

    def worker() -> None:
        nonlocal context, binding
        while not shutdown.is_set():
            item = turn_queue.get()
            if item is None:
                turn_queue.task_done()
                break
            query, echo_user = item
            running.set()
            emit("agent_start", phase="preparing")
            _interrupted = False
            try:
                with state_lock:
                    context, _interrupted, binding = _run_user_turn(query, history, context, binding, echo_user=echo_user)
                    _emit_status(context, binding, history, running=False)
            except Exception as exc:
                emit("error", text=f"Turn failed: {exc}")
            finally:
                clear_cancel()
                running.clear()
                emit("agent_end", status="interrupted" if _interrupted else "done")
                turn_queue.task_done()

    worker_thread = threading.Thread(target=worker, name="harness-event-stream-worker", daemon=True)
    worker_thread.start()

    _emit_status(context, binding, history, running=False)
    emit("ready")

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
            if ctype in ("user_message", "slash_command"):
                text = str(command.get("text") or command.get("command") or "").strip()
                if not text:
                    continue
                if running.is_set() or not turn_queue.empty():
                    emit("log", level="warn", text="Agent is already running. Press Ctrl+C to interrupt before sending another message.")
                    continue
                silent = bool(command.get("silent"))
                try:
                    turn_queue.put_nowait((text, not silent))
                except queue.Full:
                    emit("log", level="warn", text="Agent is already running. Message not queued.")
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
