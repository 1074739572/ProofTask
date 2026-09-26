"""Run the offline permission/resume boundary experiment as regression."""

from __future__ import annotations

from evals.harness_reliability.permission_boundary_experiment import run_cases


def test_permission_boundary_experiment_cases_all_pass() -> None:
    results = run_cases()
    failures = [f"{row['case']}: {row['actual']}" for row in results if not row["passed"]]
    assert not failures, "boundary cases failed:\n" + "\n".join(failures)
