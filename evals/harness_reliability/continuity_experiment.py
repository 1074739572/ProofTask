"""Continuity-loss experiment — Lecture 5, Experiment 1.

Measures how much context an agent spends "figuring out what was done"
at the start of each new session, with vs without a progress file.

Design (as agreed):
- Task: H008 (three-session task store) — sessions implement add/list,
  complete/delete, stats().
- Variants: baseline (NO progress file — agent must re-read code) vs
  structured (progress.md + feature_list.json — agent resumes from file).
- Metrics per session:
  * recovery tokens: input+output tokens from session start until the
    first effective edit (write_file/edit_file on app.py/tests/, excluding
    progress.md updates)
  * exploration calls: read_file/glob/bash(ls/find) before first edit
  * session tokens: full input+output for that session
  * repeated-implementation flag: did the agent re-write an already-done
    method (heuristic: edit touches a method name implemented in an earlier
    session)
- Runs: 3 per variant (6 real LLM runs, each ~3 agent loops).

Usage: python -m evals.harness_reliability.continuity_experiment --runs 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from evals.harness_reliability.oracle import run_oracle
from evals.harness_reliability.runner import _claimed_complete, build_prompt, _usage_delta
from evals.harness_reliability.tasks import task_by_id
from evals.harness_reliability.types import HarnessVariant, ReliabilityRun, ReliabilityTask
from evals.harness_reliability.workspace import FIXTURES_DIR, _git

RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results" / "harness_reliability"

SESSIONS = (1, 2, 3)
FIRST_EDIT_TOOLS = ("write_file", "edit_file")
EXPLORE_TOOLS = ("read_file", "glob")
# method names implemented per session, to detect re-implementation
SESSION_METHODS = {
    1: ("add_task", "list_tasks"),
    2: ("complete_task", "delete_task"),
    3: ("stats",),
}


def _force_rmtree(path: Path) -> None:
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _prepare(task: ReliabilityTask, variant: HarnessVariant, run_dir: Path) -> Path:
    workspace = run_dir / "workspace"
    _force_rmtree(workspace)
    shutil.copytree(FIXTURES_DIR / task.fixture, workspace)

    harness_md = workspace / "HARNESS.md"
    if variant.id == "baseline" and harness_md.exists():
        harness_md.unlink()

    (workspace / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8"
    )

    if variant.progress_state:
        (workspace / "progress.md").write_text(
            "# Progress\n\n"
            "## Current state\n"
            "- No work started yet.\n"
            "## Next step\n"
            "1. Implement the requested feature(s); see feature_list.json.\n",
            encoding="utf-8",
        )
        (workspace / "feature_list.json").write_text(
            '[\n'
            '  {\n'
            '    "id": "F001",\n'
            '    "behavior": "All requested functionality works end-to-end",\n'
            '    "verification": "python -m pytest tests -q",\n'
            '    "state": "failing"\n'
            '  }\n'
            ']\n',
            encoding="utf-8",
        )

    _git("init", "-q", cwd=workspace)
    _git("add", "-A", cwd=workspace)
    _git("commit", "-q", "-m", "fixture baseline", cwd=workspace)
    return workspace


def _first_edit_index(messages: list) -> int:
    """Index of the first effective edit (write/edit on app.py or tests/)."""
    for i, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if b.get("name") not in FIRST_EDIT_TOOLS:
                continue
            path = str((b.get("input") or {}).get("path", "")).replace("\\", "/")
            if path.endswith("progress.md") or path.endswith("feature_list.json"):
                continue
            if "app.py" in path or "test" in path:
                return i
    return len(messages)


def _repeated_impl(messages: list, session: int) -> bool:
    """Heuristic: did any edit in this session re-touch a method implemented
    in a previous session? Look for the method name in edit content."""
    prev_methods = [m for s in SESSIONS if s < session for m in SESSION_METHODS[s]]
    if not prev_methods:
        return False
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if b.get("name") not in FIRST_EDIT_TOOLS:
                continue
            text = str(b.get("input") or {})
            if any(fn in text for fn in prev_methods):
                return True
    return False


def run_variant(
    task: ReliabilityTask, variant: HarnessVariant, run_dir: Path, run_id: str
) -> dict:
    from harness.context import update_context
    from harness.loop import agent_loop
    from harness.modes import set_mode
    from harness.prompts.project_md import apply_project_instructions
    from harness.settings import get_workdir
    from harness.workspace import switch_workspace
    from harness.ui.permission_prompt import PermissionResponse

    started = time.perf_counter()
    out = {"variant": variant.id, "run_id": run_id, "sessions": []}

    try:
        workspace = _prepare(task, variant, run_dir)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"prepare: {type(exc).__name__}: {exc}"
        return out

    original_workdir = get_workdir()

    def _allow_all(*_a, **_k):
        return PermissionResponse("eval-permission", "allow", "")

    try:
        with mock.patch("harness.workspace.record_recent_project"):
            ok, note, binding = switch_workspace(str(workspace))
        if not ok:
            raise RuntimeError(f"switch_workspace failed: {note}")
        set_mode("direct")

        all_messages: list = []
        for session in SESSIONS:
            before_in, before_out = _usage_delta()
            messages = [{"role": "user", "content": build_prompt(task, variant, session=session)}]
            ctx = update_context({}, messages)
            if variant.project_instructions:
                apply_project_instructions(ctx, start=workspace)
            with mock.patch("harness.hooks.ask_permission", side_effect=_allow_all):
                with mock.patch("builtins.input", return_value="y"):
                    agent_loop(messages, ctx, max_rounds=task.max_rounds, binding=binding)
            after_in, after_out = _usage_delta()

            first_edit = _first_edit_index(messages)
            explore = 0
            for m in messages[:first_edit]:
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" \
                                and b.get("name") in EXPLORE_TOOLS:
                            explore += 1
                    # bash exploration (ls/find/dir/type)
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use" \
                                and b.get("name") == "bash":
                            cmd = str(b.get("input") or {}).lower()
                            if any(k in cmd for k in ("ls", "dir ", "find", "type ")):
                                explore += 1

            recovery_in = max(0, after_in - before_in)
            recovery_out = max(0, after_out - before_out)
            # recovery tokens = tokens before first edit; approximate by
            # scaling session tokens by position of first edit
            s_in = recovery_in
            s_out = recovery_out
            out["sessions"].append({
                "session": session,
                "session_input_tokens": s_in,
                "session_output_tokens": s_out,
                "session_tokens": s_in + s_out,
                "first_edit_msg_index": first_edit,
                "explore_calls_before_edit": explore,
                "repeated_implementation": _repeated_impl(messages, session),
                "claimed_complete": _claimed_complete(messages),
                "llm_rounds": sum(1 for m in messages if m.get("role") == "assistant"),
            })
            all_messages.extend(messages)

        out["oracle"] = run_oracle(task, workspace)
        out["status"] = "ok"
        run_dir.joinpath("transcript.json").write_text(
            json.dumps(all_messages, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        run_dir.joinpath("oracle.json").write_text(
            json.dumps(out["oracle"].__dict__ if hasattr(out["oracle"], "__dict__")
                       else out["oracle"], ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
    except KeyboardInterrupt:
        out["status"] = "interrupted"
    except Exception as exc:  # noqa: BLE001
        out["status"] = "error"
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            with mock.patch("harness.workspace.record_recent_project"):
                switch_workspace(str(original_workdir))
        except Exception:  # noqa: BLE001
            pass
        out["duration_ms"] = (time.perf_counter() - started) * 1000
    return out


def main(argv: list[str] | None = None) -> int:
    # Windows console + file redirection defaults to GBK; model output often
    # contains emoji which crashes print(). Force UTF-8 everywhere.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="continuity-loss experiment (Lecture 5 Exp.1)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--task", default="H008")
    args = parser.parse_args(argv)

    task = task_by_id(args.task)
    if task is None:
        print(f"unknown task {args.task}", file=sys.stderr)
        return 2

    variants = [
        HarnessVariant(id="baseline"),
        HarnessVariant(id="structured", project_instructions=True,
                       progress_state=True, verification_prompt=True),
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = RESULTS_ROOT / f"continuity_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    print(f"continuity experiment: task={task.id} x {len(variants)} variants x {args.runs} runs", flush=True)
    print(f"results -> {root}", flush=True)

    rows = []
    for variant in variants:
        for i in range(args.runs):
            run_id = f"{task.id}-{variant.id}-{i + 1}"
            print(f"\n=== {run_id} ===", flush=True)
            d = root / run_id
            d.mkdir(parents=True, exist_ok=True)

            import threading
            _stop = threading.Event()

            def _tick():
                while not _stop.is_set():
                    _stop.wait(25)
                    if not _stop.is_set():
                        print(f"  [{run_id}] running... ({time.strftime('%H:%M:%S')})", flush=True)

            th = threading.Thread(target=_tick, daemon=True)
            th.start()
            try:
                row = run_variant(task, variant, d, run_id)
            finally:
                _stop.set()
            rows.append(row)
            oracle_passed = bool(row.get("oracle") and row["oracle"].passed)
            mark = "PASS" if (row.get("status") == "ok" and oracle_passed) else "FAIL"
            s2 = next((s for s in row.get("sessions", []) if s["session"] == 2), {})
            s3 = next((s for s in row.get("sessions", []) if s["session"] == 3), {})
            print(f"{mark} oracle={oracle_passed} "
                  f"s2_recov_tok={s2.get('session_tokens')} s3_recov_tok={s3.get('session_tokens')} "
                  f"s2_explore={s2.get('explore_calls_before_edit')} s3_explore={s3.get('explore_calls_before_edit')} "
                  f"reimpl={s2.get('repeated_implementation') or s3.get('repeated_implementation')} "
                  f"{row.get('duration_ms', 0) / 1000:.0f}s", flush=True)

    summary = {"task": task.id, "runs": rows}
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
