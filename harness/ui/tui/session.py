"""Run one agent turn for the Textual UI (worker thread)."""

from __future__ import annotations

from harness.agent.cancel import clear_cancel, request_cancel, is_cancelled
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.loop import agent_lock, agent_loop
from harness.messages.repair import repair_tool_pairing
from harness.models import get_model, model_label
from harness.project.resume import checkpoint_history
from harness.project.session_undo import abort_inflight_turn
from harness.prompts.lookup import is_lookup_query
from harness.prompts.writing import is_writing_query
from harness.rag.bootstrap import bootstrap_message, ensure_rag_indexed
from harness.teams import consume_lead_inbox
from harness.ui.renderer import renderer
from harness.ui.tui.bridge import BRIDGE


def run_agent_turn(history: list, context: dict, query: str) -> dict:
    """Execute one user turn. Returns {interrupted, redo_query, message}."""
    from harness.cli import print_turn_assistants
    from harness.project.session_registry import touch_session_title_from_query

    BRIDGE.reset_turn(user_query=query, model=model_label(get_model()))
    BRIDGE.set_busy(True)
    BRIDGE.push_status("Running… (Esc / Stop)")

    from harness.rag.file_mode import handle_file_mode_turn, is_file_mode

    if is_file_mode():
        # File mode bypasses the agent loop: retrieve → grounded answer.
        # Must use renderer.assistant (push_final), not plain (push_step):
        # plain would split the answer into muted step lines and never mark
        # an assistant bubble, so it looks like "no document search happened".
        touch_session_title_from_query(query)
        renderer.user(query)
        BRIDGE.push_status("检索文档中…")
        answer = handle_file_mode_turn(query)
        renderer.assistant(answer)
        BRIDGE.seal_turn_bubbles()
        BRIDGE.push_status("Ready · file")
        BRIDGE.set_busy(False)
        BRIDGE.refresh_usage()
        return {
            "interrupted": False,
            "redo_query": None,
            "message": "",
            "context": context,
        }

    from harness.modes import note_user_query_for_mode

    gate_note = note_user_query_for_mode(query)
    if gate_note:
        BRIDGE.push_status(gate_note)
        renderer.muted(gate_note)

    hook_result = trigger_hooks("UserPromptSubmit", query)
    model_query = hook_result if isinstance(hook_result, str) else query

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

    clear_cancel()
    interrupted = False
    try:
        with agent_lock:
            try:
                interrupted = agent_loop(history, context, turn_start=turn_start)
            except KeyboardInterrupt:
                request_cancel()
                interrupted = True
    finally:
        pass

    redo_query = None
    message = ""
    if interrupted or is_cancelled():
        interrupted = True
        message, rolled_back = abort_inflight_turn(history, turn_start)
        context.update(update_context(context, history))
        if rolled_back:
            redo_query = rolled_back
        # U1: tear Chat bubbles for this turn; status only (no extra chat spam)
        BRIDGE.trim_turn_bubbles()
        BRIDGE.push_status(message or "Interrupted — edit & resend")
    else:
        with agent_lock:
            context.update(update_context(context, history))
        print_turn_assistants(history, turn_start)
        checkpoint_history(history)
        BRIDGE.seal_turn_bubbles()
        BRIDGE.push_status("Ready")

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

    clear_cancel()
    BRIDGE.set_busy(False)
    BRIDGE.refresh_usage()
    return {
        "interrupted": interrupted,
        "redo_query": redo_query,
        "message": message,
        "context": context,
    }
