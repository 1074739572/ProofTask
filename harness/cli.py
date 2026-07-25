"""Interactive CLI entry for the improved harness."""

from __future__ import annotations

import threading
import time

import harness.console as console
from harness import terminal_state
from harness.agent.cancel import clear_cancel, request_cancel
from harness.agent.cron import consume_cron_queue
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.loop import agent_loop, agent_lock
from harness.mcp.pool import bootstrap_mcp_servers, mcp_bootstrap_warnings
from harness.messages.repair import repair_tool_pairing
from harness.models import handle_model_command
from harness.modes import format_mode_status, set_mode
from harness.modes.registry import format_mode_catalog
from harness.ui.mode_picker import run_mode_picker
from harness.project.resume import (
    checkpoint_history,
    inject_project_context,
    resume_banner,
    run_resume_command,
    should_auto_inject_project_on_startup,
)
from harness.project.session_store import bootstrap_session
from harness.prompts.project_md import apply_project_instructions
from harness.tasks import reconcile_task_board
from harness.todos.state import load_todos_from_disk
from harness.project.session_undo import abort_inflight_turn, undo_last_turn
from harness.project.tools import (
    run_project_clear,
    run_project_import_transcript,
    run_project_list_transcripts,
    run_project_status,
)
from harness.teams import consume_lead_inbox
from harness.ui.banner import BANNER_STYLES, get_banner_style, print_hero, run_banner_demo
from harness.ui.interrupt_listener import InterruptListener
from harness.ui.model_picker import run_model_picker
from harness.ui.prompt_input import read_cli_query
from harness.ui.renderer import renderer
from harness.ui.welcome import render_welcome
from harness.usage import handle_usage_command


def _match_cli_command(query: str, command: str) -> bool:
    """True for `/cmd` or `/cmd args` — avoids `/model` matching `/mode`."""
    text = query.strip().lower()
    command = command.lower()
    return text == command or text.startswith(command + " ")


def _help_text() -> str:
    return """Commands:
  /model                   pick model (↑↓ Enter) or /model <id>
  /mode [id]               pick mode (↑↓ Enter): direct|plan|orchestrate|file|grill
  /mode file               文档问答：每句检索 files/；进模式时选指定/全部文档
  /mode grill              拷问模式：内置 grill-me，确认执行前不改代码
  /usage [today|week|month|year|YYYY-MM-DD|YYYY-MM|YYYY]
                           local token stats + hit rate (bars; kept across /clear)
  /undo                    cancel last completed question + reply
  Esc / Ctrl+C             stop in-flight turn; roll back to edit/resend question
  /resume                   list sessions (name · created)
  /resume <N>               switch to session N (e.g. /resume 2)
  /resume delete <N>        delete session N from list
  /resume delete project    delete long-task state.json
  /resume project           inject thesis state.json (long workflow)
  /skill                    list skills
  /skill <name>             inject skill full text into this session (then ask)
  /clear [session]          end session (keep dir); default also deletes state.json
  /clear session            new session id; keep state.json
  /import-transcript [path] [full|merge]
  /transcripts             list .transcripts backups
  /rag [files|docs|pick|select|index|add|ask|status|help]  RAG corpus + Q&A
  /banner [style|demo]     preview welcome art (classic|emoji|typewriter|shadow3d)
  /help                    this message
  q, exit                  quit"""


def _assistant_text_blocks(content) -> list[str]:
    from harness.ui.final_answer import assistant_text_blocks

    return assistant_text_blocks(content)


def print_turn_assistants(messages: list, turn_start: int | None) -> None:
    """Print final assistant prose for this turn (skip tool-rounds already narrated live).

    After context compact, ``messages`` is rewritten and may be shorter than the
    pre-turn ``turn_start`` index. Resolve to the latest real user turn so the
    final answer is still printed (otherwise the UI looks blank even though the
    model already replied into history).

    Prefer the in-loop ``emit_final_assistant`` path; this is a safety net for
    callers that do not go through that path (and skips already-printed msgs).
    """
    from harness.project.session_undo import resolve_turn_start

    resolved = resolve_turn_start(messages, turn_start)
    if resolved is None:
        return
    for msg in messages[resolved:]:
        if msg.get("role") != "assistant":
            continue
        if msg.get("_ui_final_printed"):
            continue
        content = msg.get("content")
        # Tool rounds: intent text was already shown via renderer.tool_intent during the loop.
        if isinstance(content, list) and any(
            (isinstance(b, dict) and b.get("type") == "tool_use")
            or (getattr(b, "type", None) == "tool_use")
            for b in content
        ):
            continue
        for text in _assistant_text_blocks(content):
            console.terminal_print(text)


def cron_autorun_loop(history: list, context: dict) -> None:
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append(
                    {"role": "user", "content": f"[Scheduled] {job.prompt}"}
                )
                renderer.hook("cron auto", job.prompt[:60])
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)
            checkpoint_history(history)


def bootstrap_cli_session(
    *,
    welcome: bool = True,
    start_cron: bool = True,
    cli_active: bool = True,
) -> tuple[list, dict]:
    """Shared session bootstrap for classic CLI and Textual TUI."""
    terminal_state.CLI_ACTIVE = cli_active

    history, session_source = bootstrap_session()
    load_todos_from_disk()
    reconciled = reconcile_task_board()
    if reconciled:
        renderer.warn(
            f"Archived {reconciled} completed task(s) left on the active board from a prior run."
        )
    _, repair_fixes = repair_tool_pairing(history)
    if repair_fixes:
        checkpoint_history(history)
        renderer.warn(f"Repaired {repair_fixes} broken tool message(s) in saved session.")
    context = update_context({}, history if history else [])
    if session_source:
        context["session_source"] = session_source
    project_md = apply_project_instructions(context)

    if welcome:
        render_welcome(session_source=session_source)
        # UI-only: loaded source, none, disabled, or truncated — not sent to the model.
        if project_md.truncated:
            renderer.warn(project_md.status)
        else:
            renderer.muted(project_md.status)

        banner = resume_banner()
        if banner:
            renderer.plain(banner)
            print()

    if should_auto_inject_project_on_startup():
        ok, note = inject_project_context(history, checkpoint=True)
        if ok:
            renderer.info(note)
            context = update_context(context, history)
            # update_context spreads existing keys; re-assert after rebuilds.
            apply_project_instructions(context)

    bootstrap_results = bootstrap_mcp_servers()
    for line in mcp_bootstrap_warnings(bootstrap_results):
        renderer.warn(line)
    try:
        from harness.providers.netcheck import proxy_health_warning

        warn = proxy_health_warning()
        if warn:
            renderer.warn(warn)
    except Exception:
        pass
    if start_cron:
        threading.Thread(
            target=cron_autorun_loop, args=(history, context), daemon=True
        ).start()
    return history, context


def run_cli() -> None:
    history, context = bootstrap_cli_session()

    redo_query: str | None = None

    while True:
        try:
            query = read_cli_query(redo=redo_query)
            redo_query = None
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if _match_cli_command(query, "/model"):
            parts = query.strip().split(maxsplit=1)
            if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker"):
                renderer.plain(run_model_picker())
            else:
                renderer.plain(handle_model_command(query))
            print()
            continue
        if _match_cli_command(query, "/mode"):
            parts = query.strip().split(maxsplit=1)
            if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker"):
                renderer.plain(run_mode_picker())
            elif parts[1].lower() == "help":
                renderer.plain(format_mode_catalog())
            else:
                renderer.plain(set_mode(parts[1]))
            print()
            continue
        if _match_cli_command(query, "/usage"):
            renderer.plain(handle_usage_command(query))
            print()
            continue
        if query.strip().lower() in ("/undo", "/u"):
            with agent_lock:
                ok, message = undo_last_turn(history)
                context = update_context(context, history)
            renderer.plain(message)
            print()
            continue
        if query.strip().lower() in ("/project",):
            renderer.plain(run_project_status())
            print()
            continue
        if _match_cli_command(query, "/resume"):
            parts = query.strip().split(maxsplit=1)
            sub = parts[1] if len(parts) > 1 else ""
            with agent_lock:
                note = run_resume_command(sub, messages=history)
                repair_tool_pairing(history)
                context = update_context(context, history)
                renderer.plain(note)
            print()
            continue
        if _match_cli_command(query, "/skill"):
            from harness.skills_loader import run_skill_command

            parts = query.strip().split(maxsplit=1)
            sub = parts[1] if len(parts) > 1 else ""
            with agent_lock:
                note = run_skill_command(sub, messages=history)
                repair_tool_pairing(history)
                context = update_context(context, history)
                renderer.plain(note)
            print()
            continue
        if _match_cli_command(query, "/clear"):
            parts = query.strip().split(maxsplit=1)
            sub = parts[1].lower() if len(parts) > 1 else ""
            # OpenCode 模式：默认全清（session + todos + state.json）
            # /clear session  → 只清对话，保留 state.json
            keep_project = sub in ("session", "chat", "history")
            renderer.plain(run_project_clear(clear_project=not keep_project))
            history.clear()
            context = update_context({}, [])
            project_md = apply_project_instructions(context)
            if project_md.truncated:
                renderer.warn(project_md.status)
            else:
                renderer.muted(project_md.status)
            print()
            continue
        if query.strip().lower() in ("/transcripts", "/list-transcripts"):
            renderer.plain(run_project_list_transcripts())
            print()
            continue
        if _match_cli_command(query, "/rag"):
            from harness.rag.commands import run_rag_cli_command

            renderer.plain(run_rag_cli_command(query))
            print()
            continue
        if query.strip().lower() in ("/help",):
            renderer.plain(_help_text())
            print()
            continue
        if query.strip().lower().startswith("/banner"):
            parts = query.strip().split()
            if len(parts) == 1 or parts[1].lower() == "demo":
                from rich.console import Console

                run_banner_demo(Console(highlight=False, legacy_windows=False))
            elif parts[1].lower() in BANNER_STYLES:
                from rich.console import Console

                c = Console(highlight=False, legacy_windows=False)
                print_hero(c, style=parts[1].lower(), width=c.size.width)  # type: ignore[arg-type]
            else:
                renderer.plain(
                    "Banner styles: " + ", ".join(BANNER_STYLES) + "\n"
                    "Usage: /banner demo  |  /banner emoji\n"
                    "Default: HARNESS_BANNER env (current: "
                    f"{get_banner_style()})"
                )
            print()
            continue
        if query.strip().lower().startswith("/import-transcript"):
            parts = query.strip().split(maxsplit=2)
            mode = "summary"
            path_arg = ""
            if len(parts) > 1:
                arg = parts[1]
                if arg.lower() == "full":
                    mode = "full"
                elif arg.lower() == "merge":
                    mode = "summary"
                else:
                    path_arg = arg
            if len(parts) > 2 and parts[2].lower() == "full":
                mode = "full"
            merge = "merge" in query.lower()
            renderer.plain(
                run_project_import_transcript(path=path_arg, mode=mode, merge=merge)
            )
            history[:] = bootstrap_session()[0]
            context = update_context(context, history)
            print()
            continue
        from harness.modes import get_mode, note_user_query_for_mode
        from harness.rag.file_mode import handle_file_mode_turn, is_file_mode

        # File mode: every normal message is document Q&A (RAG → answer).
        # Slash commands above still work; exit with /mode direct.
        if is_file_mode() or get_mode() == "file":
            # Same path as TUI: always RAG → assistant answer (not plain dump).
            renderer.user(query)
            renderer.assistant(handle_file_mode_turn(query))
            print()
            continue

        gate_note = note_user_query_for_mode(query)
        if gate_note:
            renderer.muted(gate_note)

        hook_result = trigger_hooks("UserPromptSubmit", query)
        # user_prompt_hook may return an augmented query (e.g. lookup-mode
        # constraint appended). Show the user's original wording on screen,
        # but send the augmented version to the model.
        model_query = hook_result if isinstance(hook_result, str) else query
        from harness.project.session_registry import touch_session_title_from_query

        from harness.prompts.lookup import is_lookup_query
        from harness.prompts.writing import is_writing_query
        from harness.rag.bootstrap import bootstrap_message, ensure_rag_indexed

        touch_session_title_from_query(query)
        repair_tool_pairing(history)
        renderer.user(query)
        turn_start = len(history)
        history.append({"role": "user", "content": model_query})
        context["latest_user_query"] = query
        context["lookup_mode"] = is_lookup_query(query)
        context["writing_mode"] = is_writing_query(query) and not context["lookup_mode"]
        context.pop("rag_bootstrap", None)
        if context["writing_mode"]:
            boot = ensure_rag_indexed("files")
            context["rag_bootstrap"] = bootstrap_message(boot)
            if boot.get("ok"):
                renderer.muted(context["rag_bootstrap"].split("\n")[0])
            else:
                renderer.warn(context["rag_bootstrap"][:200])

        listener = InterruptListener()
        interrupted = False

        def _on_interrupt() -> None:
            request_cancel()
            renderer.warn("Stopping… (Esc or Ctrl+C)")

        listener.start(_on_interrupt)
        clear_cancel()
        try:
            with agent_lock:
                try:
                    interrupted = agent_loop(history, context, turn_start=turn_start)
                except KeyboardInterrupt:
                    request_cancel()
                    interrupted = True
        finally:
            listener.stop()
            clear_cancel()

        if interrupted:
            message, rolled_back = abort_inflight_turn(history, turn_start)
            renderer.plain(message)
            context = update_context(context, history)
            if rolled_back:
                redo_query = rolled_back
        else:
            with agent_lock:
                context = update_context(context, history)
            print_turn_assistants(history, turn_start)
            checkpoint_history(history)

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:

            def inbox_label(msg: dict) -> str:
                req_id = msg.get("metadata", {}).get("request_id", "")
                suffix = f" req:{req_id}" if req_id else ""
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = "\n".join(
                f"From {m['from']} [{inbox_label(m)}]: {m['content'][:200]}"
                for m in inbox
            )
            history.append({"role": "user", "content": f"[Inbox]\n{inbox_text}"})
            checkpoint_history(history)
        print()
