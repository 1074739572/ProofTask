"""Aggregate per-run results into comparable metrics.

Primary metric: actual completion rate (oracle). The agent's own completion
claim is tracked separately so we can compute the false-completion rate
(the "verification gap" from the course).
"""

from __future__ import annotations

from evals.harness_reliability.types import ReliabilityRun


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


def _oracle_passed(run: ReliabilityRun) -> bool:
    return bool(run.oracle.passed if run.oracle else run.oracle_passed)


def summarize_runs(runs: list[ReliabilityRun]) -> dict:
    scored = [r for r in runs if r.status == "ok"]
    claimed = [r for r in scored if r.claimed_complete]
    false_claimed = [r for r in claimed if not _oracle_passed(r)]
    overreach = [r for r in scored if r.oracle and r.oracle.overreach]

    return {
        "n_runs": len(runs),
        "n_scored": len(scored),
        "n_error": sum(1 for r in runs if r.status == "error"),
        "n_interrupted": sum(1 for r in runs if r.status == "interrupted"),
        "completion_rate": _pct(sum(1 for r in scored if _oracle_passed(r)), len(scored)),
        "claimed_rate": _pct(len(claimed), len(scored)),
        "false_completion_rate": _pct(len(false_claimed), len(claimed)),
        "overreach_rate": _pct(len(overreach), len(scored)),
        "avg_llm_rounds": _avg([float(r.llm_rounds) for r in scored]),
        "avg_tool_calls": _avg([float(r.tool_calls) for r in scored]),
        "avg_input_tokens": _avg([float(r.input_tokens) for r in scored]),
        "avg_output_tokens": _avg([float(r.output_tokens) for r in scored]),
        "avg_duration_s": _avg([r.duration_ms / 1000 for r in scored]),
        "permission_denials_total": sum(r.permission_denials for r in scored),
        "failure_reasons": _top_failure_reasons(scored, limit=5),
    }


def _top_failure_reasons(runs: list[ReliabilityRun], *, limit: int) -> list[str]:
    counts: dict[str, int] = {}
    for r in runs:
        if r.oracle_passed or not r.oracle:
            continue
        for reason in r.oracle.failure_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [f"{reason} (x{n})" for reason, n in ordered[:limit]]


def group_by_variant(runs: list[ReliabilityRun]) -> dict[str, dict]:
    by_variant: dict[str, list[ReliabilityRun]] = {}
    for r in runs:
        by_variant.setdefault(r.variant_id, []).append(r)
    return {vid: summarize_runs(vruns) for vid, vruns in sorted(by_variant.items())}
