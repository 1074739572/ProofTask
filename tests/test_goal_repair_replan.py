"""Regression coverage for Goal repair replanning boundaries."""

from __future__ import annotations

import json

from harness.goal.planner import build_plan_prompt, plan_tasks
from harness.verification import select_adapter
from harness.verification.catalog import TestCatalog


MANIFEST = {
    "repo_files": ["src/app.py", "tests/test_app.py", "docs/requirements.md"],
    "evidence": [
        {"id": "E1", "path": "src/app.py", "claim": "existing application entry"},
        {"id": "E2", "path": "docs/requirements.md", "claim": "requested behavior"},
    ],
}


def _plan() -> dict:
    return {
        "goal_contract": {
            "summary": "Add an observable rate limit.",
            "constraints": ["Keep existing API compatibility."],
            "assumptions": [],
            "unresolved": [],
            "verification_preconditions": [],
            "decision_ledger": [{
                "id": "D1", "decision": "Keep the limit in the application entry.",
                "rationale": "The entry owns request handling.", "evidence_refs": ["E1"],
            }],
        },
        "tasks": [{
            "name": "enforce rate limit",
            "behavior": "Reject requests after the configured limit.",
            "acceptance_cases": [{
                "id": "AC1", "given": "a caller used the limit", "when": "another request arrives",
                "then": "the request is rejected",
            }],
            "depends_on": [],
            "primary_write": ["src/app.py"],
            "planned_new": [],
            "conditional_write": [],
            "read_envelope": ["src", "tests"],
            "forbidden": [".env"],
            "evidence_refs": ["E1", "E2"],
            "test_strategy": "Generate a focused request-limit regression test.",
            "test_selectors": [],
        }],
    }


def test_execution_replan_prompt_includes_boundary_failure_evidence():
    prompt = build_plan_prompt(
        "add a shell sandbox",
        "pytest -q",
        TestCatalog(),
        MANIFEST,
        frozen_contract=_plan()["goal_contract"],
        replan_reason="AC4 requires a worker route owned by a pending Task.",
    )

    assert "Replan trigger evidence" in prompt
    assert "AC4 requires a worker route owned by a pending Task." in prompt
    assert "Every acceptance case assigned" in prompt


def test_plan_tasks_accepts_an_explicit_adapter_without_importing_it_locally(tmp_path):
    result = plan_tasks(
        "add a rate limit",
        "pytest -q",
        tmp_path,
        planner_runner=lambda **_: json.dumps(_plan()),
        reviewer_runner=lambda **_: '{"approved":true,"summary":"executable","findings":[]}',
        discovery_manifest=MANIFEST,
        test_catalog=TestCatalog(),
        verification_adapter=select_adapter(tmp_path, "pytest -q"),
    )

    assert result.tasks
