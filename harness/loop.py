"""Core agent loop: one loop, full harness."""

from __future__ import annotations

import threading

from harness.agent.background import (
    build_user_content,
    inject_background_notifications,
    should_run_background,
    start_background_task,
)
from harness.agent.cancel import is_cancelled
from harness.agent.compact import compact_history, prepare_context, reactive_compact
from harness.agent.cron import consume_cron_queue
from harness.agent.recovery import (
    RecoveryState,
    is_prompt_too_long_error,
    with_retry,
)
from harness.agent.grounding_guard import GroundingGuard
from harness.agent.repeat_guard import RepeatGuard
from harness.agent.lookup_guard import LOOKUP_FORCE_ANSWER, LookupGuard
from harness.agent.writing_guard import WritingGuard
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.llm import create_message
from harness.messages.blocks import block_field, block_text, has_displayable_text, is_text, is_tool_use
from harness.messages.repair import finalize_cancelled_tool_round, repair_tool_pairing
from harness.models import get_model
from harness.project.session import serialize_messages
from harness.prompts import assemble_static_system_prompt, messages_with_ephemeral_context
from harness.settings import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
)
from harness.tools.dispatch import call_tool_handler, has_tool_use
from harness.tools.registry import get_tool_pool
from harness.todos.format import format_todo_reminder
from harness.todos.state import note_llm_round_without_todo_update, rounds_since_todo_update
from harness.ui.renderer import renderer
from harness.ui.turn_summary import TurnMutationTracker

agent_lock = threading.Lock()


def _append_assistant(messages: list, content) -> None:
    messages.append(serialize_messages([{"role": "assistant", "content": content}])[0])


def _publish_context_metrics(messages: list) -> None:
    from harness.ui.tui.mode import is_tui_active

    if not is_tui_active():
        return
    from harness.agent.compact import estimate_tokens, model_context_window
    from harness.ui.tui.bridge import BRIDGE

    BRIDGE.push_context_usage(
        estimate_tokens(messages),
        model_context_window(get_model()),
    )


def call_llm(messages: list, context: dict, tools: list, state: RecoveryState, max_tokens: int):
    # Optional per-run override (rare). Prefer shared identity + lookup constraints
    # over swapping personas for evals — see harness.prompts.lookup.
    system = context.get("system_override") or assemble_static_system_prompt()
    api_messages = messages_with_ephemeral_context(messages, context)
    model_id = state.fallback_model or get_model()
    return with_retry(
        lambda: create_message(
            model_id=model_id,
            system=system,
            messages=api_messages,
            tools=tools,
            max_tokens=max_tokens,
        ),
        state,
    )


def agent_loop(
    messages: list,
    context: dict,
    *,
    turn_start: int | None = None,
    max_rounds: int | None = None,
) -> bool:
    """Run until the agent finishes or cancel is requested. Returns True if interrupted.

    max_rounds: optional cap on LLM turns (used by evals). None = unlimited.
    """
    from harness.prompts.ephemeral import reset_ephemeral_cache

    reset_ephemeral_cache()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS
    llm_rounds = 0
    repeat_guard = RepeatGuard()
    grounding_guard = GroundingGuard()
    # lookup_mode (CLI) or web_budget (e.g. GAIA eval) both enable fetch caps
    lookup_guard = LookupGuard(
        active=bool(context.get("lookup_mode") or context.get("web_budget"))
    )
    writing_guard = WritingGuard(active=bool(context.get("writing_mode")))
    mutations = TurnMutationTracker()

    def _finish(interrupted: bool) -> bool:
        if mutations.paths:
            renderer.files_changed(mutations.paths)
        return interrupted

    while True:
        if is_cancelled():
            return _finish(True)

        if max_rounds is not None and llm_rounds >= max_rounds:
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Stopped] reached max_rounds={max_rounds}",
                        }
                    ],
                }
            )
            trigger_hooks("Stop", messages)
            return _finish(False)

        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            renderer.hook("cron inject", job.prompt[:60])

        inject_background_notifications(messages)

        if rounds_since_todo_update >= 3:
            messages.append(
                {"role": "user", "content": format_todo_reminder()}
            )

        prepare_context(messages)
        _publish_context_metrics(messages)
        repair_tool_pairing(messages)
        context = update_context(context, messages)
        tools, handlers = get_tool_pool()
        if state.strip_tools_until_answer:
            tools, handlers = [], {}

        try:
            llm_rounds += 1
            response = call_llm(messages, context, tools, state, max_tokens)
        except Exception as exc:
            if is_cancelled():
                return _finish(True)
            if is_prompt_too_long_error(exc) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            from harness.providers.errors import format_api_error

            formatted = format_api_error(exc)
            error_msg = {
                "role": "assistant",
                "content": [{"type": "text", "text": f"[Error] {formatted}"}],
                # Prevent print_turn_assistants from re-emitting via assistant().
                "_ui_final_printed": True,
            }
            messages.append(error_msg)
            # TUI: sticky error dock; classic CLI: styled error line.
            renderer.error(formatted)
            return _finish(False)

        if is_cancelled():
            return _finish(True)

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                renderer.warn(f"max_tokens: retry with {max_tokens}")
                continue
            _append_assistant(messages, response.content)
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return _finish(False)

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        _append_assistant(messages, response.content)
        if not has_tool_use(response.content):
            if (
                not has_displayable_text(response.content)
                and not state.has_nudged_empty_reply
            ):
                state.has_nudged_empty_reply = True
                renderer.warn(
                    "模型本轮没有可见文字回复（可能只有内部推理），正在请求文字总结…"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[Harness] Your last assistant turn had no user-visible "
                            "text reply. Answer the user's latest question now in "
                            "plain text. Do not call any tools."
                        ),
                    }
                )
                continue
            # Print final answer here so it is never lost if CLI post-loop
            # work is interrupted or drowned by permission/encoding noise.
            from harness.ui.final_answer import emit_final_assistant

            emit_final_assistant(messages, response.content)
            trigger_hooks("Stop", messages)
            state.strip_tools_until_answer = False
            return _finish(False)

        results = []
        compacted_now = False
        had_todo_write = False
        force_lookup_finalize = False
        grounding_block, grounding_msg = grounding_guard.evaluate(
            messages, response.content
        )
        for block in response.content:
            if is_cancelled():
                finalize_cancelled_tool_round(messages, response.content, results)
                return _finish(True)
            # Show the model's brief "why" text before tools (human UI only).
            if is_text(block):
                text = block_text(block).strip()
                if text:
                    renderer.tool_intent(text)
                continue
            if not is_tool_use(block):
                continue
            name = block_field(block, "name", "")
            tool_input = block_field(block, "input", {}) or {}
            tool_use_id = str(block_field(block, "id", "") or "")

            if grounding_block:
                renderer.tool_repeat(
                    name,
                    tool_input if isinstance(tool_input, dict) else None,
                    streak=1,
                    blocked=True,
                    tool_use_id=tool_use_id,
                )
                renderer.tool_result(
                    grounding_msg,
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": grounding_msg,
                    }
                )
                continue

            streak, should_block = repeat_guard.note(
                name, tool_input if isinstance(tool_input, dict) else {}
            )
            if streak > 1:
                renderer.tool_repeat(
                    name,
                    tool_input if isinstance(tool_input, dict) else None,
                    streak=streak,
                    blocked=should_block,
                    tool_use_id=tool_use_id,
                )
            else:
                renderer.tool_start(
                    name,
                    tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )

            if name == "compact":
                messages[:] = compact_history(messages)
                messages.append(
                    {
                        "role": "user",
                        "content": "[Compacted. Continue with summarized context.]",
                    }
                )
                compacted_now = True
                break

            if should_block and name != "compact":
                output = repeat_guard.block_message(name, streak)
                renderer.tool_result(
                    output,
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": output,
                    }
                )
                continue

            lookup_block, lookup_msg = lookup_guard.check_before_fetch(
                name, tool_input if isinstance(tool_input, dict) else None
            )
            if lookup_block:
                if lookup_guard.note_block():
                    force_lookup_finalize = True
                renderer.tool_repeat(
                    name,
                    tool_input if isinstance(tool_input, dict) else None,
                    streak=lookup_guard.fetch_count + 1,
                    blocked=True,
                    tool_use_id=tool_use_id,
                )
                renderer.tool_result(
                    lookup_msg,
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": lookup_msg,
                    }
                )
                continue

            write_block, write_msg = writing_guard.check_write(
                name, tool_input if isinstance(tool_input, dict) else None
            )
            if write_block:
                renderer.tool_repeat(
                    name,
                    tool_input if isinstance(tool_input, dict) else None,
                    streak=1,
                    blocked=True,
                    tool_use_id=tool_use_id,
                )
                renderer.tool_result(
                    write_msg,
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": write_msg,
                    }
                )
                continue

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                renderer.tool_result(
                    str(blocked),
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": str(blocked),
                    }
                )
                # Esc during Allow? [y/N] sets cancel — exit the turn now.
                if is_cancelled():
                    finalize_cancelled_tool_round(messages, response.content, results)
                    return _finish(True)
                continue

            if should_run_background(name, tool_input):
                bg_id = start_background_task(block, handlers)
                output = (
                    f"[Background task {bg_id} started] "
                    "Result will arrive as a task_notification."
                )
                renderer.tool_result(
                    output,
                    name=name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_use_id=tool_use_id,
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": output,
                    }
                )
                continue

            handler = handlers.get(name)
            lookup_guard.note_fetch(name, tool_input if isinstance(tool_input, dict) else None)
            output = call_tool_handler(handler, tool_input, name)
            lookup_guard.note_result(
                name, tool_input if isinstance(tool_input, dict) else None, str(output)
            )
            writing_guard.note_tool(name)
            # Mid-budget nudge: once we've used >=60% of the web budget, inject a
            # one-shot reminder so the agent starts converging instead of
            # fetching more. Only fires once per turn.
            if (
                lookup_guard.active
                and lookup_guard.max_fetches is not None
                and lookup_guard.max_fetches > 2
                and not state.has_nudged_web_budget
                and lookup_guard.fetch_count
                >= max(2, int(lookup_guard.max_fetches * 0.6))
            ):
                state.has_nudged_web_budget = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Harness] You've used {lookup_guard.fetch_count} of "
                            f"{lookup_guard.max_fetches} web tool calls. Wrap up: "
                            "synthesize what you have and produce the final answer. "
                            "Only fetch again if a single, specific source is clearly "
                            "missing. Prefer `FINAL ANSWER: ...` if requested."
                        ),
                    }
                )
            mutations.note(
                name,
                tool_input if isinstance(tool_input, dict) else None,
                output,
            )
            trigger_hooks("PostToolUse", block, output)
            renderer.tool_result(
                str(output),
                name=name,
                tool_input=tool_input if isinstance(tool_input, dict) else None,
                tool_use_id=tool_use_id,
            )
            if name == "todo_write":
                had_todo_write = True
                from harness.prompts.ephemeral import reset_ephemeral_cache

                reset_ephemeral_cache()

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": output,
                }
            )

        if compacted_now:
            continue

        if is_cancelled():
            finalize_cancelled_tool_round(messages, response.content, results)
            return _finish(True)

        if not had_todo_write:
            note_llm_round_without_todo_update()

        messages.append({"role": "user", "content": build_user_content(results)})
        if force_lookup_finalize and not state.has_lookup_force_finalize:
            state.has_lookup_force_finalize = True
            state.strip_tools_until_answer = True
            messages.append({"role": "user", "content": LOOKUP_FORCE_ANSWER})
            renderer.warn("LookupGuard: forcing answer (no more web tools)")
