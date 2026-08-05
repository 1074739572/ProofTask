"""CLI: python -m evals.harness_reliability [--task H001] [--variant baseline] [--runs 1]

Runs the real agent (with API) against controlled fixtures and compares
harness configurations. Results go to evals/results/harness_reliability/.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.harness_reliability.report import format_report, save_report
from evals.harness_reliability.runner import run_single
from evals.harness_reliability.tasks import TASKS, VARIANTS, task_by_id, variant_by_id

RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results" / "harness_reliability"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="improved_harness reliability eval")
    parser.add_argument(
        "--task",
        action="append",
        dest="task_ids",
        help="Task id (repeatable; default: all enabled tasks)",
    )
    parser.add_argument(
        "--variant",
        action="append",
        dest="variant_ids",
        help="Variant id: baseline|instructions|structured (default: all)",
    )
    parser.add_argument("--runs", type=int, default=1, help="Repeats per (task, variant)")
    args = parser.parse_args(argv)

    tasks = [task_by_id(t) for t in (args.task_ids or [])]
    tasks = [t for t in tasks if t is not None] or TASKS
    variants = [variant_by_id(v) for v in (args.variant_ids or [])]
    variants = [v for v in variants if v is not None] or VARIANTS

    if not tasks or not variants:
        print("No tasks/variants selected", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / f"run_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"harness reliability eval: {len(tasks)} task(s) x {len(variants)} variant(s)"
          f" x {args.runs} run(s)", flush=True)
    print(f"results -> {run_dir}", flush=True)

    runs = []
    for task in tasks:
        for variant in variants:
            for i in range(args.runs):
                run_id = f"{task.id}-{variant.id}-{i + 1}"
                print(f"\n=== {run_id} ===", flush=True)
                task_dir = run_dir / run_id
                task_dir.mkdir(parents=True, exist_ok=True)
                run = run_single(task, variant, task_dir, run_id)
                runs.append(run)
                oracle_passed = bool(run.oracle.passed if run.oracle else run.oracle_passed)
                mark = "PASS" if (run.status == "ok" and oracle_passed) else "FAIL"
                print(f"{mark} oracle_passed={oracle_passed} "
                      f"claimed={run.claimed_complete} "
                      f"rounds={run.llm_rounds} tools={run.tool_calls} "
                      f"tokens={run.input_tokens + run.output_tokens} "
                      f"{run.duration_ms / 1000:.0f}s"
                      + (f"  error={run.error[:120]}" if run.error else ""), flush=True)

    summary_path = save_report(run_dir, runs)
    print()
    print(format_report(runs))
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
