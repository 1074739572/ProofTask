"""Terminal report + JSON persistence for harness reliability evals."""

from __future__ import annotations

import json
from pathlib import Path

from evals.harness_reliability.metrics import group_by_variant, summarize_runs
from evals.harness_reliability.types import ReliabilityRun


def format_report(runs: list[ReliabilityRun]) -> str:
    lines = [
        "=" * 68,
        " harness reliability eval",
        "=" * 68,
    ]
    for r in runs:
        passed = bool(r.oracle.passed if r.oracle else r.oracle_passed)
        mark = "PASS" if (r.status == "ok" and passed) else "FAIL"
        claimed = "claimed=Y" if r.claimed_complete else "claimed=N"
        lines.append(
            f"  {mark:4} {r.task_id:<8} {r.variant_id:<14} {claimed:<10} "
            f"rounds={r.llm_rounds:<3} tools={r.tool_calls:<3} "
            f"tok={r.input_tokens + r.output_tokens:<6} {r.duration_ms / 1000:.0f}s"
        )
        if r.status != "ok":
            lines.append(f"        error: {r.error[:160]}")
        elif not r.oracle_passed and r.oracle:
            for reason in r.oracle.failure_reasons[:3]:
                lines.append(f"        {reason[:160]}")

    lines.append("")
    lines.append("-- per variant --")
    for vid, summary in group_by_variant(runs).items():
        lines.append(
            f"  {vid:<14} completion={summary['completion_rate']:>5.1f}%  "
            f"false_completion={summary['false_completion_rate']:>5.1f}%  "
            f"overreach={summary['overreach_rate']:>5.1f}%  "
            f"avg_tools={summary['avg_tool_calls']}  avg_tokens={summary['avg_input_tokens']}"
        )
    lines.append("")
    return "\n".join(lines)


def save_report(run_dir: Path, runs: list[ReliabilityRun]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "overall": summarize_runs(runs),
        "by_variant": group_by_variant(runs),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in runs) + "\n",
        encoding="utf-8",
    )
    return run_dir / "summary.json"
