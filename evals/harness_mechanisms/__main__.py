"""Run all M-series mechanism evals. Usage: python -m evals.harness_mechanisms"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.harness_mechanisms.cases.m001_bash_stdin import run_m001
from evals.harness_mechanisms.types import MResult, summarize

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results" / "harness_mechanisms"


def _run_single(case_fn, *, rounds: int = 1) -> int:
    """Run one mechanism case and print its result. Returns 0 if passed."""
    result = case_fn(rounds=rounds)
    mark = "PASS" if result.passed else "FAIL"
    print("=" * 60)
    print(" harness mechanisms eval (M-series)")
    print("=" * 60)
    print(f"  {mark}  {result.id:<6} {result.name}")
    if result.detail:
        print(f"        {result.detail}")
    print("=" * 60)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {"generated_at": stamp, "rounds": max(1, rounds), **summarize([result])}
    path = RESULTS_DIR / f"run_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    return 0 if result.passed else 1


def all_cases(rounds: int) -> list[MResult]:
    return [run_m001(rounds=rounds)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="improved_harness mechanism evals (M-series)")
    parser.add_argument("--rounds", type=int, default=1, help="Attempts per mechanism (default 1)")
    args = parser.parse_args(argv)

    results = all_cases(rounds=max(1, args.rounds))
    summary = summarize(results)

    print("=" * 60)
    print(" harness mechanisms eval (M-series)")
    print("=" * 60)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  {r.id:<6} {r.name}")
        if r.detail:
            print(f"        {r.detail}")
    print(f"  score={summary['passed']}/{summary['n']} "
          f"passed={summary['passed']} failed={summary['failed']}")
    print("=" * 60)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": stamp,
        "rounds": max(1, args.rounds),
        **summary,
    }
    path = RESULTS_DIR / f"run_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
