"""Core agent loop: one loop, full harness."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

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
    candidate_recovery_models,
    is_model_recoverable_error,
    is_prompt_too_long_error,
    with_retry,
)
from harness.agent.grounding_guard import GroundingGuard
from harness.agent.repeat_guard import RepeatGuard
from harness.agent.writing_guard import WritingGuard
from harness.context import update_context
from harness.hooks import trigger_hooks
from harness.llm import create_message
from harness.mcp.pool import ensure_mcp_ready, take_mcp_bootstrap_warnings
from harness.messages.blocks import block_field, block_text, has_displayable_text, is_text, is_tool_use
from harness.messages.injections import is_harness_injection_text
from harness.messages.repair import finalize_cancelled_tool_round, repair_tool_pairing
from harness.models import get_model
from harness.modes import mode_auto_route, mode_enables_task, mode_lead_model_hint
from harness.project.session import serialize_messages
from harness.prompts import assemble_system_prompt
from harness.settings import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
)
from harness.tools.dispatch import call_tool_handler, has_tool_use
from harness.tools.registry import get_tool_pool
from harness.todos.format import format_todo_reminder
from harness.todos import state as todo_state
from harness.ui.renderer import renderer
from harness.ui.turn_summary import TurnMutationTracker

agent_lock = threading.RLock()


@dataclass
class LoopStats:
    """Minimal per-run statistics for callers like the /goal runner.

    ``stop_reason`` is one of: completed / max_rounds / cancelled / error /
    max_tokens. Backward compatible: existing callers keep receiving the
    boolean return value.
    """

    interrupted: bool = False
    llm_rounds: int = 0
    stop_reason: str = "completed"


def _append_assistant(messages: list, content) -> None:
    messages.append(serialize_messages([{"role": "assistant", "content": content}])[0])


def _latest_plain_user(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            text = content.strip()
            if not is_harness_injection_text(text):
                return text
    return ""


def _local_failure_summary(messages: list, errors: list[str], *, attempted_models: list[str]) -> str:
    from harness.ui.classic_display import render_failure_summary

    teammate_notes: list[str] = []
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, str) and content.startswith("[Inbox]"):
            teammate_notes.append(content.replace("[Inbox]", "").strip())
            if len(teammate_notes) >= 3:
                break
    return render_failure_summary(
        user_query=_latest_plain_user(messages),
        errors=errors,
        attempted_models=attempted_models,
        teammate_notes=teammate_notes,
    )


def call_llm(
    messages: list,
    context: dict,
    tools: list,
    state: RecoveryState,
    max_tokens: int,
    *,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
    usage_context: dict[str, str] | None = None,
):
    # Optional per-run override (rare). Prefer shared identity + lookup constraints
    # over swapping personas for evals — see harness.prompts.lookup.
    system = assemble_system_prompt(
        context,
        base_system=context.get("system_override"),
    )
    # The provider sees only the durable conversation history. Session/task
    # instructions live in ``system``; volatile state arrives through tools or
    # event messages when it actually changes.
    api_messages = list(messages)
    # Subagent-enabled modes may bind a coordinator model independently from
    # the user's direct model selection. Recovery fallback remains highest priority.
    lead_model = mode_lead_model_hint() if mode_enables_task() else None
    selected_model = state.fallback_model or lead_model or model_id or get_model()
    # A recovery model may have a different effort vocabulary. Its configured
    # default is safer than forwarding an override meant for the primary model.
    selected_effort = None if state.fallback_model else reasoning_effort
    return with_retry(
        lambda: create_message(
            model_id=selected_model,
            reasoning_effort=selected_effort,
            system=system,
            messages=api_messages,
            tools=tools,
            max_tokens=max_tokens,
            usage_context=usage_context,
        ),
        state,
    )


def agent_loop(
    messages: list,
    context: dict,
    *,
    turn_start: int | None = None,
    max_rounds: int | None = None,
    binding = None,
    disabled_tools: set[str] | None = None,
    stats: LoopStats | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
) -> bool:
    """Run until the agent finishes or cancel is requested. Returns True if interrupted.

    max_rounds: optional cap on LLM turns (used by evals). When None the cap is
    read from HARNESS_MAX_LLM_ROUNDS (default 200) so an agent looping on tools
    cannot spin forever; pass max_rounds=0 to disable the cap entirely.
    binding: optional SessionBinding for persisting compact boundaries.
    disabled_tools: optional tool-name set hidden from the model this run
    (both schema and handler are filtered — /goal ACT mode).
    stats: optional LoopStats filled with interrupt/round/stop-reason info.
    """
    from harness.prompts.ephemeral import reset_ephemeral_cache

    # MCP connects in the background at startup; the first turn waits briefly
    # so the tool pool includes MCP tools before the first LLM call. Failures
    # are surfaced here (once), not during startup.
    ensure_mcp_ready()
    for line in take_mcp_bootstrap_warnings():
        renderer.warn(line)

    if max_rounds is None:
        raw = os.getenv("HARNESS_MAX_LLM_ROUNDS", "200").strip()
        try:
            max_rounds = int(raw)
        except ValueError:
            max_rounds = 200
        if max_rounds <= 0:
            max_rounds = None  # 0 disables the cap (explicit opt-out)

    reset_ephemeral_cache()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS
    llm_rounds = 0
    repeat_guard = RepeatGuard()
    grounding_guard = GroundingGuard()
    writing_guard = WritingGuard(active=bool(context.get("writing_mode")))
    mutations = TurnMutationTracker()
    model_overrides = {
        key: value
        for key, value in {
            "model_id": model_id,
            "reasoning_effort": reasoning_effort,
        }.items()
        if value is not None
    }

    def _finish(interrupted: bool, reason: str = "completed") -> bool:
        if mutations.paths:
            renderer.files_changed(mutations.paths)
        if stats is not None:
            stats.interrupted = interrupted
            stats.llm_rounds = llm_rounds
            stats.stop_reason = reason
        return interrupted

    while True:
        if is_cancelled():
            return _finish(True, "cancelled")

        if max_rounds is not None and llm_rounds >= max_rounds:
            note = (
                f"[Stopped] reached max_rounds={max_rounds}. The agent looked "
                f"stuck in a loop, so the turn was stopped. Send a new message "
                f"to continue, or raise HARNESS_MAX_LLM_ROUNDS."
            )
            renderer.warn(note)
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": note}],
                }
            )
            trigger_hooks("Stop", messages)
            return _finish(False, "max_rounds")

        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            renderer.hook("cron inject", job.prompt[:60])

        inject_background_notifications(messages)

        if todo_state.rounds_since_todo_update >= 3:
            messages.append(
                {"role": "user", "content": format_todo_reminder()}
            )

        prepare_context(messages, binding=binding)
        repair_tool_pairing(messages)
        context = update_context(context, messages)
        effective_disabled = set(disabled_tools or ())
        # Automatic lookup intent is a capability boundary, not just prompt
        # prose.  Keep the user's selected mode intact while hiding mutation
        # and shell tools for this turn.
        if context.get("lookup_active"):
            effective_disabled.update(
                {"bash", "write_file", "edit_file", "patch_file", "rag_index",
                 "task", "spawn_teammate", "create_worktree", "remove_worktree",
                 "project_init", "project_note", "project_reset", "project_set_chapter"}
            )
        tools, handlers = get_tool_pool(disabled_tools=effective_disabled)
        tool_schemas = {
            str(tool.get("name")): tool.get("input_schema")
            for tool in tools
            if tool.get("name")
        }

        # Auto-route: when enabled, classify and dispatch the latest user
        # message to a sub-agent before the lead model sees it.
        if llm_rounds == 0 and mode_auto_route():
            from harness.modes.routing import route_user_message

            if route_user_message(messages):
                # Routing happened; re-prepare context with the injected result.
                context = update_context(context, messages)
                tools, handlers = get_tool_pool(disabled_tools=effective_disabled)
                tool_schemas = {
                    str(tool.get("name")): tool.get("input_schema")
                    for tool in tools
                    if tool.get("name")
                }

        try:
            llm_rounds += 1
            primary_usage_context = (
                {"session_id": str(getattr(binding, "session_id", "") or "")}
                if binding is not None
                else None
            )
            call_overrides = dict(model_overrides)
            if primary_usage_context is not None:
                call_overrides["usage_context"] = primary_usage_context
            response = call_llm(
                messages, context, tools, state, max_tokens,
                **call_overrides,
            )
        except Exception as exc:
            if is_cancelled():
                return _finish(True, "cancelled")
            if is_prompt_too_long_error(exc) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages, binding=binding)
                state.has_attempted_reactive_compact = True
                continue
            from harness.providers.errors import format_api_error

            formatted = format_api_error(exc)
            lead_model = mode_lead_model_hint() if mode_enables_task() else None
            active_model = state.fallback_model or lead_model or model_id or get_model()
            attempted_models = [active_model]
            if is_model_recoverable_error(exc):
                for candidate in candidate_recovery_models(active_model):
                    if candidate in attempted_models:
                        continue
                    renderer.warn(f"模型调用失败，尝试恢复模型：{candidate}")
                    state.fallback_model = candidate
                    attempted_models.append(candidate)
                    try:
                        response = call_llm(
                            messages, context, tools, state, max_tokens,
                            **call_overrides,
                        )
                        break
                    except Exception as retry_exc:
                        formatted = format_api_error(retry_exc)
                        continue
                else:
                    response = None
                if response is not None:
                    # Continue normal processing with the recovered response.
                    pass
                else:
                    fallback_text = _local_failure_summary(
                        messages,
                        [formatted],
                        attempted_models=attempted_models,
                    )
                    messages.append(
                        {"role": "assistant", "content": [{"type": "text", "text": fallback_text}]}
                    )
                    renderer.error(fallback_text)
                    return _finish(False, "error")
            else:
                fallback_text = _local_failure_summary(
                    messages,
                    [formatted],
                    attempted_models=attempted_models,
                )
                messages.append(
                    {"role": "assistant", "content": [{"type": "text", "text": fallback_text}]}
                )
                renderer.error(fallback_text)
                return _finish(False, "error")

        if is_cancelled():
            return _finish(True, "cancelled")

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
            return _finish(False, "max_tokens")

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
            return _finish(False)

        results = []
        compacted_now = False
        had_todo_write = False
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
                messages[:] = compact_history(messages, binding=binding)
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
                    return _finish(True, "cancelled")
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
            from harness.tools.dispatch import validate_tool_input
            input_error = validate_tool_input(tool_schemas.get(name), tool_input)
            if input_error:
                output = f"Tool input error ({name}): {input_error}"
            elif name == "bash" and isinstance(tool_input, dict):
                # Stream stdout/stderr line-by-line as tool_output events so the
                # TUI shows live output (Claude Code style) instead of a black
                # box that resolves when the command finishes.
                from harness.tools.filesystem import run_bash_streaming

                output = run_bash_streaming(
                    command=str(tool_input.get("command", "")),
                    cwd=Path(tool_input["cwd"]) if tool_input.get("cwd") else None,
                    timeout=tool_input.get("timeout"),
                    tool_use_id=tool_use_id,
                )
            else:
                # Input was validated immediately above; keep the historical
                # three-argument call shape for embedders that monkeypatch the
                # dispatcher.
                output = call_tool_handler(handler, tool_input, name)
            writing_guard.note_tool(name)
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
            return _finish(True, "cancelled")

        if not had_todo_write:
            todo_state.note_llm_round_without_todo_update()

        messages.append({"role": "user", "content": build_user_content(results)})
