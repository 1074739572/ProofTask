"""Slash-command handlers for the Textual TUI."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.ui.tui.app import HarnessApp


def _match(query: str, command: str) -> bool:
    text = query.strip().lower()
    command = command.lower()
    return text == command or text.startswith(command + " ")


def dispatch_slash(app: HarnessApp, query: str) -> bool:
    """Handle a slash/meta command. Returns True if consumed."""
    q = query.strip()
    if not q:
        return False

    if q.lower() in ("/exit", "/quit", "exit", "quit", "q"):
        app.exit()
        return True

    if q.lower() == "/classic":
        app.tui_set_status("Restart with: python main.py --classic")
        app.chat_append(
            "system",
            "Classic Rich CLI: python main.py --classic",
        )
        return True

    if _match(q, "/help") or q.lower() == "help":
        app.chat_append("assistant", _help_md())
        app.tui_set_status("Help")
        return True

    if _match(q, "/model"):
        if _busy_block(app):
            return True
        _handle_model(app, q)
        return True

    if _match(q, "/mode"):
        if _busy_block(app):
            return True
        _handle_mode(app, q)
        return True

    if _match(q, "/resume"):
        if _busy_block(app):
            return True
        _handle_resume(app, q)
        return True

    if _match(q, "/clear"):
        if _busy_block(app):
            return True
        _handle_clear(app, q)
        return True

    if _match(q, "/skill"):
        if _busy_block(app):
            return True
        _handle_skill(app, q)
        return True

    if _match(q, "/rag"):
        if _busy_block(app):
            return True
        app._run_rag_command(q)
        return True

    if _match(q, "/usage"):
        from harness.usage.report import handle_usage_command

        app.chat_append("assistant", handle_usage_command(q))
        app.refresh_usage_bar()
        app.tui_set_status("Usage refreshed")
        return True

    if q.startswith("/"):
        app.chat_append(
            "system",
            f"`{q}` not in TUI yet. Built-in: /model /mode /rag /resume /skill /clear /usage /help /classic /quit. "
            "Or: python main.py --classic",
        )
        app.tui_set_status("Unknown command")
        return True

    return False


def _busy_block(app: HarnessApp) -> bool:
    if getattr(app, "_busy", False):
        app.tui_set_status("Stop the current turn first")
        return True
    return False


def _help_md() -> str:
    return """# Commands (TUI)

| Command | Action |
|---------|--------|
| `/model` | Pick model (or click 🤖) |
| `/model <id>` | Switch by id |
| `/mode` | Pick mode (or click 🧭) |
| `/mode <id>` | Switch mode (`direct` / `plan` / `orchestrate` / `file` / `grill`) |
| `/mode grill` | Builtin grill-me: ask → clarify → confirm → execute |
| `/rag` | 本地 files/ 清单 + 索引状态 |
| `/rag files` | 看上传了哪些文件（已索引/未索引） |
| `/rag docs` / `/rag select 1,3` | 已索引文档列表 / 设检索范围 |
| `/rag pick` | 交互多选文档（Space 勾选） |
| `/rag index [path]` | 构建或刷新索引 |
| `/rag ask <question>` | 一次性文档问答 |
| `/resume` | List sessions + picker |
| `/resume <N>` | Switch to session N |
| `/resume project` | Inject thesis state.json |
| `/resume delete <N>` | Delete session N |
| `/skill` | List skills + picker |
| `/skill <name>` | Inject skill into session (then ask) |
| `/clear` | Clear session (+ project by default) |
| `/clear session` | Clear chat only, keep state.json |
| `/usage [today|week|month|year]` | Token usage and cache hit rate |
| `/help` | This help |
| `/classic` | Rich CLI hint |
| `/quit` | Exit (Ctrl+Q) |

**Composer:** Enter = send · Shift+Enter = newline · Esc = stop · Ctrl+C = copy last answer to OS clipboard · Ctrl+V = paste · Quit = Ctrl+Q

**Layout:** top = usage · middle = chat · under chat = model/mode/status · bottom = input
"""


def _apply_history_side_effects(app: HarnessApp) -> None:
    from harness.context import update_context
    from harness.messages.repair import repair_tool_pairing

    repair_tool_pairing(app.history)
    app.context = update_context(app.context, app.history)


def _handle_resume(app: HarnessApp, query: str) -> None:
    from harness.loop import agent_lock
    from harness.project.resume import format_resume_status, run_resume_command
    from harness.project.session_registry import visible_session_summaries

    parts = query.strip().split(maxsplit=1)
    sub = parts[1] if len(parts) > 1 else ""
    sub_low = sub.strip().lower()

    # Bare /resume → list + inline picker (same rows as classic /resume N)
    if sub_low in ("", "list", "status", "session", "pick", "picker"):
        note = format_resume_status(include_project=True)
        app.chat_append("system", note)
        rows = visible_session_summaries()
        if rows:
            _open_resume_picker(app, rows)
            app.tui_set_status("Pick a session · Esc keep current")
        else:
            app.tui_set_status("No sessions yet")
        return

    with agent_lock:
        note = run_resume_command(sub, messages=app.history)
        _apply_history_side_effects(app)

    app.reload_session_view()
    app.chat_append("system", note)
    app.tui_set_status(_short_status(note))


def _open_resume_picker(app: HarnessApp, rows: list[dict]) -> None:
    from harness.loop import agent_lock
    from harness.project.resume import switch_to_session

    labels: list[str] = []
    ids: list[str] = []
    cursor = 0
    for index, row in enumerate(rows):
        mark = "  ← 当前" if row.get("active") else ""
        created = row.get("created_at") or row.get("updated_at") or 0
        ts = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created)))
            if created
            else "—"
        )
        title = row.get("title") or "(untitled)"
        labels.append(f"{index + 1}. {title}  ·  {ts}{mark}")
        ids.append(str(row["id"]))
        if row.get("active"):
            cursor = index

    def on_pick(session_id: str | None) -> None:
        if not session_id:
            app.tui_set_status("Kept current session")
            return
        row = next((r for r in rows if r["id"] == session_id), None)
        if row is None:
            app.tui_set_status("Session not found")
            return
        if row.get("active"):
            app.tui_set_status(f"Already on: {row.get('title') or session_id}")
            return
        with agent_lock:
            note = switch_to_session(session_id, app.history)
            _apply_history_side_effects(app)
        app.reload_session_view()
        app.chat_append("system", note)
        app.tui_set_status(_short_status(note))

    app.open_inline_picker(
        "Select session",
        labels,
        ids,
        initial_index=cursor,
        on_pick=on_pick,
    )


def _handle_clear(app: HarnessApp, query: str) -> None:
    from harness.loop import agent_lock
    from harness.project.tools import run_project_clear
    from harness.prompts.project_md import apply_project_instructions

    parts = query.strip().split(maxsplit=1)
    sub = parts[1].lower() if len(parts) > 1 else ""
    keep_project = sub in ("session", "chat", "history")

    with agent_lock:
        note = run_project_clear(clear_project=not keep_project)
        app.history.clear()
        from harness.context import update_context

        app.context = update_context({}, [])
        md = apply_project_instructions(app.context)

    app.reload_session_view()
    app.chat_append("system", note)
    status = md.status if md.status else (_short_status(note) or "Cleared")
    app.tui_set_status(status)

def _handle_skill(app: HarnessApp, query: str) -> None:
    from harness.loop import agent_lock
    from harness.skills_loader import (
        format_skill_command_status,
        inject_skill,
        scan_skills,
        skill_names,
    )

    parts = query.strip().split(maxsplit=1)
    sub = parts[1] if len(parts) > 1 else ""
    sub_low = sub.strip().lower()

    if sub_low in ("", "list", "status", "ls", "pick", "picker"):
        note = format_skill_command_status()
        app.chat_append("system", note)
        scan_skills()
        names = skill_names()
        if names:
            _open_skill_picker(app, names)
            app.tui_set_status("Pick a skill · Esc cancel")
        else:
            app.tui_set_status("No skills yet")
        return

    with agent_lock:
        ok, note = inject_skill(sub.strip(), app.history, checkpoint=True)
        _apply_history_side_effects(app)

    # D1: short notice only — full body stays in history for the model.
    app.chat_append("system", note)
    app.tui_set_status(note if ok else _short_status(note))


def _open_skill_picker(app: HarnessApp, names: list[str]) -> None:
    from harness.loop import agent_lock
    from harness.skills_loader import SKILL_REGISTRY, inject_skill

    labels: list[str] = []
    for name in names:
        desc = (SKILL_REGISTRY.get(name) or {}).get("description") or ""
        desc = " ".join(str(desc).split())
        if len(desc) > 48:
            desc = desc[:47] + "…"
        labels.append(f"{name}  —  {desc}" if desc else name)

    def on_pick(skill_id: str | None) -> None:
        if not skill_id:
            app.tui_set_status("Skill picker closed")
            return
        with agent_lock:
            ok, note = inject_skill(skill_id, app.history, checkpoint=True)
            _apply_history_side_effects(app)
        app.chat_append("system", note)
        app.tui_set_status(note if ok else _short_status(note))

    app.open_inline_picker(
        "Select skill",
        labels,
        names,
        initial_index=0,
        on_pick=on_pick,
    )


def _short_status(note: str) -> str:
    line = (note or "").strip().splitlines()[0] if note else ""
    if len(line) > 60:
        return line[:59] + "…"
    return line or "Done"


def _handle_model(app: HarnessApp, query: str) -> None:
    from harness.models import get_model, model_label, set_model
    from harness.ui.model_picker import menu_entries

    parts = query.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].lower() not in ("list", "pick", "picker"):
        msg = set_model(parts[1])
        app.refresh_meta_bar()
        app.tui_set_status(msg)
        app.chat_append("step", f"model → {model_label()}")
        return

    labels, model_ids, cursor = menu_entries()
    if not model_ids:
        app.tui_set_status("No models in config/models.json")
        return

    def on_pick(model_id: str | None) -> None:
        if not model_id:
            app.tui_set_status(f"Kept model: {get_model()}")
            return
        msg = set_model(model_id)
        app.refresh_meta_bar()
        app.chat_append("step", f"model → {model_label()}")
        app.tui_set_status(msg)

    app.open_inline_picker(
        "Select model",
        labels,
        model_ids,
        initial_index=cursor,
        on_pick=on_pick,
    )


def _handle_mode(app: HarnessApp, query: str) -> None:
    from harness.modes import format_mode_status, get_mode, set_mode
    from harness.modes.registry import format_mode_catalog
    from harness.ui.mode_picker import menu_entries

    def _apply_mode_result(msg: str) -> None:
        """Show mode change in chat; file/grill banners are multi-line answers."""
        app.refresh_meta_bar()
        app.chat_append("step", f"mode → {get_mode()}")
        body = (msg or "").strip()
        if not body:
            return
        # File / grill enter banners must be assistant bubbles, not status-only —
        # otherwise users miss "直接提问即可" and think RAG never runs.
        if get_mode() in ("file", "grill") or "\n" in body:
            app.chat_append("assistant", body)
        app.tui_set_status(body.splitlines()[0][:120])

    parts = query.strip().split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.lower() == "help":
            app.chat_append("assistant", format_mode_catalog())
            return
        if arg.lower() not in ("list", "pick", "picker"):
            _apply_mode_result(set_mode(arg))
            return

    labels, mode_ids, cursor = menu_entries()
    if not mode_ids:
        app.tui_set_status("No modes in config/modes.json")
        return

    def on_pick(mode_id: str | None) -> None:
        if not mode_id:
            app.tui_set_status(format_mode_status())
            return
        _apply_mode_result(set_mode(mode_id))

    app.open_inline_picker(
        "Select mode",
        labels,
        mode_ids,
        initial_index=cursor,
        on_pick=on_pick,
    )
