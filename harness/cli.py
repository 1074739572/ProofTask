"""Interactive CLI entry for the improved harness."""

from __future__ import annotations

from pathlib import Path
import threading
import time

import harness.console as console
from harness import terminal_state
from harness.agent.cancel import clear_cancel, request_cancel
from harness.agent.cron import consume_cron_queue
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.loop import agent_loop, agent_lock
from harness.mcp.pool import bootstrap_mcp_servers_async
from harness.messages.repair import repair_tool_pairing
from harness.models import handle_model_command
from harness.modes import format_mode_status, set_mode
from harness.modes.registry import format_mode_catalog
from harness.ui.mode_picker import run_mode_picker
from harness.project.resume import (
    checkpoint_history,
    inject_project_context,
    is_new_session_command,
    new_session_title,
    run_resume_command,
    should_auto_inject_project_on_startup,
    start_new_session,
)
from harness.project.session_registry import SessionBinding, touch_session_title_from_query
from harness.project.session_store import bootstrap_session
from harness.prompts.project_md import apply_project_instructions
from harness.tasks import reconcile_task_board
from harness.todos.state import load_todos_from_disk, set_binding as todos_set_binding
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


def _print_status_footer() -> None:
    """Dimmed footer bar: model · mode · today usage · mcp."""
    try:
        from datetime import date
        from harness.models import get_model
        from harness.modes import get_mode
        from harness.mcp.pool import mcp_clients
        from harness.usage.store import totals_for_day
        from harness.ui.classic_display import render_status_footer

        totals = totals_for_day(date.today())
        cache_hit_rate = totals.hit_rate if totals.calls > 0 else None
        print(
            render_status_footer(
                model=get_model(),
                mode=get_mode(),
                cache_hit_rate=cache_hit_rate,
                mcp_count=len(mcp_clients),
            )
        )
    except Exception:
        pass


def _fmt_token(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(n)


def _match_cli_command(query: str, command: str) -> bool:
    """True for `/cmd` or `/cmd args` — avoids `/model` matching `/mode`."""
    text = query.strip().lower()
    command = command.lower()
    return text == command or text.startswith(command + " ")


def handle_permission_command(query: str, session=None) -> str:
    """Handle the in-memory ``/permission`` session-mode command.

    The command intentionally lives beside the shared CLI matcher so both the
    classic terminal and the JSONL/TUI bridge can use exactly the same parsing
    and wording.  A caller may pass a :class:`PermissionSession` for isolated
    tests/embedded sessions; normal callers use the active process holder.
    """
    from harness.permission_session import (
        PERMISSION_MODES,
        get_permission_session,
    )

    active = session if session is not None else get_permission_session()
    raw = (query or "").strip()
    parts = raw.split()
    # Keep this helper useful when called directly with just an argument while
    # still treating a malformed command as a usage error.
    if not parts or parts[0].lower() != "/permission":
        return "Usage: /permission [default|auto-review|full-access]"
    args = parts[1:]
    if not args:
        current = active.get_mode() if hasattr(active, "get_mode") else active.mode
        return (
            f"Permission mode: {current}\n"
            "Available modes: default, auto-review, full-access\n"
            "Usage: /permission <mode>"
        )
    if len(args) != 1:
        current = active.get_mode() if hasattr(active, "get_mode") else active.mode
        return (
            "Usage: /permission <default|auto-review|full-access>\n"
            f"Current mode: {current}"
        )
    requested = args[0].strip().lower()
    try:
        selected = active.set_mode(requested)
    except (TypeError, ValueError):
        current = active.get_mode() if hasattr(active, "get_mode") else active.mode
        return (
            f"Unknown permission mode '{args[0]}'.\n"
            "Available modes: " + ", ".join(PERMISSION_MODES) + "\n"
            "Usage: /permission <mode>\n"
            f"Current mode: {current}"
        )
    return f"Permission mode set to: {selected}"


def _handle_permission_command(query: str, session=None) -> str:
    """Private compatibility alias used by command-routing tests."""
    return handle_permission_command(query, session=session)


def _resolve_open_directory(query: str) -> tuple[Path | None, str]:
    raw_path = query.strip()[len("/open") :].strip()
    if not raw_path:
        return None, "Usage: /open <directory>"
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return None, f"Cannot open {raw_path!r}: {exc}"
    if not resolved.is_dir():
        return None, f"Cannot open {raw_path!r}: not a directory"
    return resolved, ""


def _help_text() -> str:
    return """Commands:
  /open <directory>        switch workspace in-process (instant, no restart)
  /permission [mode]       view/set session permissions: default|auto-review|full-access
  /goal --verify "<cmd>" -- <target>   start an autonomous goal
  /goal status|pause|stop|resume|cancel control the running goal
  /init                    scan repo & create/improve HARNESS.md handbook
  /model                   pick model (↑↓ Enter) or /model <id>
  /mode [id]               pick mode (↑↓ Enter): direct|plan|orchestrate|file|grill
  /mode file               文档问答：每句检索 files/；进模式时选指定/全部文档
  /mode grill              拷问模式：内置 grill-me，确认执行前不改代码
  /usage [today|week|month|year|YYYY-MM-DD|YYYY-MM|YYYY]
                           local token stats + hit rate (bars; kept across /clear)
  /stats                   用量仪表盘：今日/7日/30日对比 + 分模型统计
  /undo                    cancel last completed question + reply
  Esc / Ctrl+C             stop in-flight turn; roll back to edit/resend question
  /resume                   list sessions (name · created)
  /resume <N>               switch to session N (e.g. /resume 2)
  /resume delete <N>        delete session N from list
  /resume delete project    delete long-task state.json
  /resume project           inject thesis state.json (long workflow)
  new [title]               start a new chat (keeps old history for /resume)
  /new [title]              same as new
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


def print_session_history(messages: list, *, limit: int = 300) -> None:
    """Render a restored transcript in the classic terminal.

    The JSONL/TUI frontend receives a ``history_replay`` event, but the classic
    line frontend has no event reducer to paint that transcript.  Previously a
    successful ``/resume`` only printed a one-line preview, which made the
    loaded session look empty even though the model received the messages.
    Keep this renderer bounded like the TUI replay and use the normal user /
    assistant renderers so Rich and plain terminals have the same formatting.
    """
    from harness.project.resume import _message_text
    from harness.ui import events

    # A JSONL caller has its own history_replay protocol.  Keeping this helper
    # silent there avoids turning restored messages into fresh user/assistant
    # events (and avoids duplicate rows in an external TUI).
    if events.is_enabled():
        return

    if not messages:
        return
    bounded_limit = max(1, int(limit))
    selected = messages[-bounded_limit:]
    rows: list[tuple[str, str]] = []
    for message in selected:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message.get("content")).strip()
        if text:
            rows.append((role, text))
    if not rows:
        return

    renderer.muted("--- 历史消息（已加载） ---")
    if len(messages) > bounded_limit:
        renderer.muted(f"… 更早的历史消息已折叠，仅显示最近 {bounded_limit} 条")
    for role, text in rows:
        if role == "user":
            renderer.user(text)
        else:
            renderer.assistant(text)
    renderer.muted("--- 历史消息结束 ---")


def cron_autorun_loop(history: list, context: dict, *, binding: SessionBinding) -> None:
    while True:
        time.sleep(1)
        # Goal owns the workspace while it is mutating/verifying. Dropping a
        # scheduled chat turn is unsafe; leave it queued until Goal finishes.
        from harness.goal.runner import is_goal_running
        if is_goal_running():
            continue
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
            agent_loop(history, context, binding=binding)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)
            checkpoint_history(history, binding=binding)


def bootstrap_cli_session(
    *,
    welcome: bool = True,
    start_cron: bool = True,
    cli_active: bool = True,
) -> tuple[list, dict, SessionBinding]:
    """Shared session bootstrap for classic CLI.

    Returns (history, context, binding).  The *binding* pins all writes for
    this process — other windows changing active_session.json don't affect it.
    """
    terminal_state.CLI_ACTIVE = cli_active

    # Permission mode is a runtime choice, never restored from a persisted
    # conversation or configuration file.  Every fresh process/session starts
    # safely in ``default``.
    from harness.permission_session import reset_permission_session

    reset_permission_session()

    history, binding, session_source = bootstrap_session()
    todos_set_binding(binding)
    load_todos_from_disk(binding=binding)
    reconciled = reconcile_task_board()
    if reconciled:
        renderer.warn(
            f"Archived {reconciled} completed task(s) left on the active board from a prior run."
        )
    _, repair_fixes = repair_tool_pairing(history)
    if repair_fixes:
        checkpoint_history(history, binding=binding)
        renderer.warn(f"Repaired {repair_fixes} broken tool message(s) in saved session.")
    context = update_context({}, history if history else [])
    if history:
        from harness.agent.question_state import remember_latest_question

        remember_latest_question(context, history)
    if session_source:
        context["session_source"] = session_source
    project_md = apply_project_instructions(context)

    if welcome:
        render_welcome(session_source=session_source)
        if project_md.truncated:
            renderer.warn(project_md.status)
        else:
            renderer.muted(project_md.status)
        # Classic CLI has no frontend reducer to consume a history replay
        # event. Paint an automatically restored transcript immediately after
        # the welcome so ``HARNESS_CONTINUE_SESSION=1`` is visibly useful.
        if cli_active and history:
            print_session_history(history)

    if should_auto_inject_project_on_startup():
        ok, note = inject_project_context(history, binding=binding, checkpoint=True)
        if ok:
            renderer.info(note)
            context = update_context(context, history)
            apply_project_instructions(context)

    bootstrap_mcp_servers_async()
    renderer.muted("MCP servers connecting in background …")
    try:
        from harness.providers.netcheck import proxy_health_warning

        warn = proxy_health_warning()
        if warn:
            renderer.warn(warn)
    except Exception:
        pass
    if start_cron:
        threading.Thread(
            target=cron_autorun_loop, args=(history, context), kwargs={"binding": binding}, daemon=True
        ).start()
    return history, context, binding


def run_cli() -> None:
    history, context, binding = bootstrap_cli_session()

    redo_query: str | None = None

    while True:
        try:
            query = read_cli_query(redo=redo_query)
            redo_query = None
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if _match_cli_command(query, "/goal"):
            from harness.goal.commands import handle_goal_command

            renderer.plain(handle_goal_command(query, history, context, binding))
            print()
            continue
        if _match_cli_command(query, "/init"):
            from harness.goal.runner import is_goal_running as _goal_running
            from harness.prompts.init_md import handle_init_command

            if _goal_running():
                renderer.warn(
                    "Cannot /init while a goal is running — use /goal pause or /goal cancel first."
                )
                print()
                continue
            renderer.plain("Scanning repository and writing HARNESS.md …")
            with agent_lock:
                try:
                    renderer.plain(handle_init_command())
                except Exception as exc:
                    renderer.error(f"/init failed: {exc}")
            print()
            continue
        if _match_cli_command(query, "/open"):
            from harness.goal.runner import is_goal_running as _goal_running

            if _goal_running():
                renderer.warn(
                    "Cannot /open while a goal is running — use /goal pause or /goal cancel first."
                )
                print()
                continue
            # In-process workspace switch (same semantics as the TUI bridge):
            # bare `/open` lists recent projects, `/open N` picks by index,
            # `/open <dir>` switches.  No process restart.
            from harness.event_stream import _emit_workspace_list, _handle_open_workspace
            from harness.todos.state import set_binding as _todos_set_binding
            from harness.workspace import switch_workspace

            note, target, list_mode = _handle_open_workspace(query)
            if list_mode:
                _emit_workspace_list()
                print()
                continue
            if target is None:
                renderer.warn(note)
                print()
                continue
            ok, result, new_binding = switch_workspace(target)
            if not ok:
                renderer.warn(result)
                print()
                continue
            if new_binding is not None:
                binding = new_binding
                _todos_set_binding(binding)
                from harness.todos.state import load_todos_from_disk

                load_todos_from_disk(binding=binding)
            history.clear()
            context = update_context({}, [])
            apply_project_instructions(context)
            from harness.permission_session import reset_permission_session

            reset_permission_session()
            renderer.plain(result)
            print()
            continue
        if _match_cli_command(query, "/model"):
            parts = query.strip().split(maxsplit=1)
            if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker"):
                renderer.plain(run_model_picker())
            else:
                renderer.plain(handle_model_command(query))
            print()
            continue
        if is_new_session_command(query):
            from harness.goal.runner import is_goal_running as _goal_running

            if _goal_running():
                renderer.warn(
                    "Cannot /new while a goal is running — use /goal pause or /goal cancel first."
                )
                print()
                continue
            with agent_lock:
                binding, note = start_new_session(
                    history,
                    binding=binding,
                    title=new_session_title(query),
                )
                context = update_context({}, history)
                apply_project_instructions(context)
            renderer.plain(note)
            print()
            continue
        if _match_cli_command(query, "/permission"):
            parts = query.strip().split(maxsplit=1)
            if len(parts) == 1:
                from harness.ui.permission_picker import run_permission_picker

                renderer.plain(run_permission_picker())
            else:
                renderer.plain(handle_permission_command(query))
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
        if _match_cli_command(query, "/stats"):
            from harness.ui.classic_display import render_stats_dashboard
            renderer.plain(render_stats_dashboard())
            print()
            continue
        if query.strip().lower() in ("/undo", "/u"):
            with agent_lock:
                ok, message = undo_last_turn(history, binding=binding)
                context = update_context(context, history)
            renderer.plain(message)
            print()
            continue
        if query.strip().lower() in ("/project",):
            renderer.plain(run_project_status())
            print()
            continue
        if _match_cli_command(query, "/resume"):
            from harness.goal.runner import is_goal_running as _goal_running

            if _goal_running():
                renderer.warn(
                    "Cannot /resume while a goal is running — use /goal pause or /goal cancel first."
                )
                print()
                continue
            parts = query.strip().split(maxsplit=1)
            sub = parts[1] if len(parts) > 1 else ""
            if not sub:
                from harness.ui.terminal_menu import select_from_list
                from harness.project.session_registry import visible_session_summaries

                sessions = visible_session_summaries(limit=20)
                if sessions:
                    labels = []
                    for idx, s in enumerate(sessions):
                        created = s.get("created_at") or s.get("updated_at") or 0
                        import time as _time
                        ts = _time.strftime("%m/%d %H:%M", _time.localtime(created)) if created else "—"
                        title = (s["title"] or "(untitled)")[:40]
                        mark = "  ← 当前" if s.get("active") else ""
                        labels.append(f"{idx+1}. {title}  ·  {ts}{mark}")
                    labels.append("✕  取消")
                    choice = select_from_list(
                        labels,
                        title="选择会话",
                        hint="↑↓ 选择 · Enter 确认 · Esc 取消",
                    )
                    if choice is not None and choice < len(sessions):
                        sub = str(choice + 1)
            with agent_lock:
                previous_binding = binding
                note, new_binding = run_resume_command(sub, messages=history, binding=binding)
                previous_id = getattr(previous_binding, "session_id", None)
                if new_binding is not None:
                    binding = new_binding
                    todos_set_binding(binding)
                    # ``/resume status`` returns the current binding and must
                    # not reset a mode; selecting/deleting into a different
                    # session starts a fresh runtime permission context.
                    new_id = getattr(new_binding, "session_id", None)
                    if new_binding is not previous_binding and new_id != previous_id:
                        from harness.permission_session import reset_permission_session

                        reset_permission_session()
                repair_tool_pairing(history)
                context = update_context(context, history)
                renderer.plain(note)
                switched = (
                    new_binding is not None
                    and getattr(new_binding, "session_id", None) != previous_id
                )
                if switched:
                    print_session_history(history)
            print()
            continue
        if _match_cli_command(query, "/skill"):
            from harness.skills_loader import run_skill_command

            parts = query.strip().split(maxsplit=1)
            sub = parts[1] if len(parts) > 1 else ""
            with agent_lock:
                note = run_skill_command(sub, messages=history, binding=binding)
                repair_tool_pairing(history)
                context = update_context(context, history)
                renderer.plain(note)
            print()
            continue
        if _match_cli_command(query, "/clear"):
            from harness.goal.runner import is_goal_running as _goal_running

            if _goal_running():
                renderer.warn(
                    "Cannot /clear while a goal is running — use /goal pause or /goal cancel first."
                )
                print()
                continue
            parts = query.strip().split(maxsplit=1)
            sub = parts[1].lower() if len(parts) > 1 else ""
            keep_project = sub in ("session", "chat", "history")
            renderer.plain(run_project_clear(clear_project=not keep_project, binding=binding))
            history.clear()
            # Start a new session binding
            from harness.project.session_registry import create_session
            binding = create_session()
            from harness.permission_session import reset_permission_session

            reset_permission_session()
            todos_set_binding(binding)
            load_todos_from_disk(binding=binding)
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
            history[:], binding, _source = bootstrap_session()
            from harness.permission_session import reset_permission_session

            reset_permission_session()
            todos_set_binding(binding)
            load_todos_from_disk(binding=binding)
            context = update_context(context, history)
            print()
            continue
        from harness.modes import get_mode, note_user_query_for_mode
        from harness.rag.file_mode import handle_file_mode_turn, is_file_mode

        from harness.goal.runner import is_goal_running as _goal_running

        if _goal_running():
            renderer.warn("Goal is running. Use /goal status|pause|stop|cancel.")
            print()
            continue

        # File mode: every normal message is document Q&A (RAG → answer).
        if is_file_mode() or get_mode() == "file":
            renderer.user(query)
            renderer.assistant(handle_file_mode_turn(query))
            print()
            continue

        gate_note = note_user_query_for_mode(query)
        if gate_note:
            renderer.muted(gate_note)

        trigger_hooks("UserPromptSubmit", query)
        model_query = query

        from harness.prompts.lookup import is_lookup_active
        from harness.prompts.writing import is_writing_query
        from harness.rag.bootstrap import bootstrap_message, ensure_rag_indexed

        touch_session_title_from_query(query, binding=binding)
        repair_tool_pairing(history)
        turn_start = len(history)
        history.append({"role": "user", "content": model_query})
        lookup_active = is_lookup_active(query)
        # The prompt constraint is accompanied by a hard per-turn capability
        # flag consumed by agent_loop; automatic lookup must not merely ask the
        # model to self-police mutation tools.
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
                    interrupted = agent_loop(history, context, turn_start=turn_start, binding=binding)
                except KeyboardInterrupt:
                    request_cancel()
                    interrupted = True
        finally:
            listener.stop()
            clear_cancel()

        if not interrupted:
            from harness.agent.question_state import remember_turn_question

            remember_turn_question(context, history, turn_start)

        if interrupted:
            message, rolled_back = abort_inflight_turn(history, turn_start, binding=binding)
            renderer.plain(message)
            context = update_context(context, history)
            if rolled_back:
                redo_query = rolled_back
        else:
            with agent_lock:
                context = update_context(context, history)
            print_turn_assistants(history, turn_start)
            checkpoint_history(history, binding=binding)

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            groups: dict[str, list[dict]] = {}
            for m in inbox:
                groups.setdefault(m["from"], []).append(m)
            tree_parts = ["Agents"]
            branch_chars = ["├─ ", "│  ", "└─ ", "   "]
            for agent_name, msgs in groups.items():
                tree_parts.append(f"  {agent_name}")
                for i, m in enumerate(msgs):
                    prefix = branch_chars[0] if i < len(msgs) - 1 else branch_chars[2]
                    indent = branch_chars[1] if i < len(msgs) - 1 else branch_chars[3]
                    content = m.get("content", "")[:120]
                    msg_type = m.get("type", "message")
                    label = f" [{msg_type}]" if msg_type != "message" else ""
                    tree_parts.append(f"  {prefix}{content}{label}")
            inbox_text = "\n".join(tree_parts)
            history.append({"role": "user", "content": f"[Inbox]\n{inbox_text}"})
            checkpoint_history(history, binding=binding)

        _print_status_footer()
        print()
