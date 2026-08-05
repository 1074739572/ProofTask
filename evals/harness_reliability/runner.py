"""Run one (task, variant) through the real agent loop and collect evidence.

Reuses the real harness (agent_loop, switch_workspace, usage ledger) — this
is not a mock. The harness code itself is treated as the system under test.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from unittest import mock

from evals.harness_reliability.oracle import git_changed_files, run_oracle
from evals.harness_reliability.types import HarnessVariant, ReliabilityRun, ReliabilityTask

_COMPLETION_WORDS = (
    "完成",
    "搞定",
    "已修复",
    "修复完成",
    "全部通过",
    "done",
    "fixed",
    "completed",
    "finished",
    "all tests pass",
    "all tests passing",
    "passed",
    "7 passed",
    "通过",
)


def _last_assistant_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "".join(texts).strip()
        else:
            text = str(content or "").strip()
        if text and not text.startswith("[Stopped]"):
            return text
    return ""


def _claimed_complete(messages: list) -> bool:
    """Heuristic: did the agent self-report completion in its final reply?"""
    text = _last_assistant_text(messages).lower()
    return any(word in text for word in _COMPLETION_WORDS)


def _count_tool_uses(messages: list) -> tuple[int, int, list[str], int]:
    tool_calls = 0
    permission_denials = 0
    files_changed: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_calls += 1
                name = block.get("name", "")
                inp = block.get("input") or {}
                if name in ("write_file", "edit_file") and inp.get("path"):
                    path = str(inp["path"]).replace("\\", "/")
                    if path not in files_changed:
                        files_changed.append(path)
            elif block.get("type") == "tool_result":
                raw = str(block.get("content", ""))
                permission_denials += raw.count("Permission denied")
    return tool_calls, permission_denials, files_changed, 0


def build_prompt(task: ReliabilityTask, variant: HarnessVariant, *, session: int = 1) -> str:
    if session == 2:
        # Session 2 continues the same workspace; structured config tells the
        # agent to read progress.md to resume, baseline just continues.
        base = task.prompt_session2 or task.prompt
        if variant.id == "structured":
            base += (
                "\n\nWork rules:\n"
                "- Start by reading progress.md to see what session 1 did.\n"
                "- Work on ONE feature at a time (WIP=1).\n"
                "- Before claiming completion, run the verification command "
                "`python -m pytest tests -q` and make sure it passes.\n"
                "- Update progress.md before finishing."
            )
        return base

    base = task.prompt
    if variant.id == "instructions":
        base += (
            "\n\nStart by reading HARNESS.md (or README.md) to learn the project's "
            "commands and definition of done. Follow the verification command "
            "listed there before claiming completion."
        )
    elif variant.id == "structured":
        base += (
            "\n\nWork rules:\n"
            "- Start by reading HARNESS.md, progress.md and feature_list.json.\n"
            "- Work on ONE feature at a time (WIP=1).\n"
            "- Before claiming completion, run the verification command "
            "`python -m pytest tests -q` and make sure it passes.\n"
            "- Update progress.md before finishing."
        )
    return base


def _usage_delta() -> tuple[int, int]:
    """Read today's usage ledger; caller computes before/after difference."""
    from harness.usage.store import totals_for_day

    totals = totals_for_day()
    return totals.input_tokens, totals.out


def run_single(
    task: ReliabilityTask,
    variant: HarnessVariant,
    run_dir: Path,
    run_id: str,
) -> ReliabilityRun:
    """Execute one run. Returns a fully populated ReliabilityRun."""
    from harness.context import update_context
    from harness.loop import agent_loop
    from harness.modes import set_mode
    from harness.prompts.project_md import apply_project_instructions
    from harness.settings import get_workdir
    from harness.workspace import switch_workspace

    started = time.perf_counter()
    run = ReliabilityRun(task_id=task.id, variant_id=variant.id, run_id=run_id)

    from evals.harness_reliability.workspace import prepare_workspace

    try:
        workspace = prepare_workspace(task, variant, run_dir)
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = f"workspace prepare: {type(exc).__name__}: {exc}"
        run.duration_ms = (time.perf_counter() - started) * 1000
        return run

    original_workdir = get_workdir()
    before_input, before_output = _usage_delta()

    try:
        # Do not pollute the user's recent-projects list with eval fixtures.
        with mock.patch("harness.workspace.record_recent_project"):
            ok, note, binding = switch_workspace(str(workspace))
        if not ok:
            raise RuntimeError(f"switch_workspace failed: {note}")

        set_mode("direct")
        messages = [{"role": "user", "content": build_prompt(task, variant)}]
        ctx = update_context({}, messages)
        if variant.project_instructions:
            apply_project_instructions(ctx, start=workspace)

        with mock.patch("builtins.input", return_value="y"):
            agent_loop(messages, ctx, max_rounds=task.max_rounds, binding=binding)

        # --- multi-session tasks: run session 2 in the same workspace ------
        if task.requires_multi_session:
            messages2 = [{"role": "user", "content": build_prompt(task, variant, session=2)}]
            ctx2 = update_context({}, messages2)
            if variant.project_instructions:
                apply_project_instructions(ctx2, start=workspace)
            with mock.patch("builtins.input", return_value="y"):
                agent_loop(messages2, ctx2, max_rounds=task.max_rounds, binding=binding)
            messages.extend(messages2)

        # --- evidence collection -------------------------------------------
        run.claimed_complete = _claimed_complete(messages)
        run.tool_calls, run.permission_denials, run.files_changed, _ = _count_tool_uses(
            messages
        )
        run.llm_rounds = sum(1 for m in messages if m.get("role") == "assistant")

        after_input, after_output = _usage_delta()
        run.input_tokens = max(0, after_input - before_input)
        run.output_tokens = max(0, after_output - before_output)

        run.oracle = run_oracle(task, workspace)
        run.status = "ok"

        # --- persist artifacts ---------------------------------------------
        run.transcript_path = str(run_dir / "transcript.json")
        (run_dir / "transcript.json").write_text(
            json.dumps(messages, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        diff = _git_diff(workspace)
        if diff:
            run.diff_path = str(run_dir / "diff.patch")
            (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
        (run_dir / "oracle.json").write_text(
            json.dumps(run.to_dict()["oracle"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except KeyboardInterrupt:
        run.status = "interrupted"
        run.error = "KeyboardInterrupt"
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        # Restore the user's workspace so the eval process never leaks state.
        try:
            with mock.patch("harness.workspace.record_recent_project"):
                switch_workspace(str(original_workdir))
        except Exception:  # noqa: BLE001
            pass
        run.duration_ms = (time.perf_counter() - started) * 1000

    return run


def _git_diff(workspace: Path) -> str:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
