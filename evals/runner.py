"""Collect and run all eval cases."""

from __future__ import annotations

import argparse
import sys
import time
import traceback

from evals.cases import compact as compact_cases
from evals.cases import clean as clean_cases
from evals.cases import evaluation as eval_cases
from evals.cases import features as feat_cases
from evals.cases import goal as goal_cases
from evals.cases import live as live_cases
from evals.cases import mechanisms as mech_cases
from evals.cases import modes as mode_cases
from evals.cases import permissions as perm_cases
from evals.cases import project_md as project_md_cases
from evals.cases import simulated as sim_cases
from evals.cases import tools_mcp as tools_cases
from evals.cases import verification as ver_cases
from evals.errors import EvalSkip, EvalWarn
from evals.report import format_report, save_report
from evals.types import EvalCase, EvalReport, EvalResult


def all_cases() -> list[EvalCase]:
    return [
        *perm_cases.CASES,
        *mode_cases.CASES,
        *tools_cases.CASES,
        *compact_cases.CASES,
        *clean_cases.CASES,
        *eval_cases.CASES,
        *sim_cases.CASES,
        *mech_cases.CASES,
        *feat_cases.CASES,
        *ver_cases.CASES,
        *goal_cases.CASES,
        *project_md_cases.CASES,
        *live_cases.CASES,
    ]


def run_case(case: EvalCase, *, live: bool) -> EvalResult:
    started = time.perf_counter()
    if case.requires_live and not live:
        return EvalResult(
            id=case.id,
            name=case.name,
            category=case.category,
            status="skip",
            detail="pass --live to run",
            duration_ms=0,
            notes=case.notes,
        )
    try:
        case.run()
        status = "pass"
        detail = ""
    except EvalWarn as exc:
        status = "warn"
        detail = str(exc)
    except EvalSkip as exc:
        status = "skip"
        detail = str(exc)
    except AssertionError as exc:
        status = "fail"
        detail = str(exc) or "assertion failed"
    except Exception as exc:
        status = "fail"
        detail = f"{type(exc).__name__}: {exc}"
        # Keep traceback for debugging hard failures
        detail += " | " + traceback.format_exc(limit=2).replace("\n", " ").strip()
    elapsed = (time.perf_counter() - started) * 1000
    return EvalResult(
        id=case.id,
        name=case.name,
        category=case.category,
        status=status,
        detail=detail[:300],
        duration_ms=elapsed,
        notes=case.notes,
    )


def run_evals(*, live: bool = False) -> EvalReport:
    report = EvalReport(live=live)
    for case in all_cases():
        report.results.append(run_case(case, live=live))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="improved_harness mini-eval")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run live LLM smoke tests (needs API key in .env)",
    )
    args = parser.parse_args(argv)
    report = run_evals(live=args.live)
    path = save_report(report)
    print(format_report(report))
    print(f"Wrote {path}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
