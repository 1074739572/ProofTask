"""E-series: independent evaluator checks (L5).

Zero-LLM assertions on the evaluator plumbing (the model itself is mocked or
not invoked):

- E001: evaluator agent registered with a valid model, NO write tools and NO bash
- E002: tools_for_agent resolves only read-only handlers for evaluator
- E003: collect_inputs includes behavior/verification/evidence/diff + UNTRACKED files
- E004: parse_findings parses a well-formed JSON verdict
- E005: parse_findings tolerates prose-wrapped / malformed output (never raises)
- E006: record_evaluation persists findings on the feature (state untouched);
       the subagent receives the feature workspace as cwd
- E007: requires_evaluation is the explicit evaluation_required flag

All tests run in temp workspaces; nothing calls an LLM.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


# --- E001 / E002: role + read-only tool pool ---------------------------------

def case_e001_evaluator_registered_readonly() -> None:
    from harness.agents.registry import get_agent_profile, list_agent_types, validate_agent_model

    assert "evaluator" in list_agent_types(), "evaluator agent missing from agents.json"
    profile = get_agent_profile("evaluator")
    assert profile is not None
    # Model must resolve (registered in models.json).
    assert validate_agent_model("evaluator") is None, validate_agent_model("evaluator")
    # Tool pool must be read-only — no write/edit AND no bash (evaluator must
    # not execute anything, not even read-only shell).
    assert "write_file" not in profile.tools
    assert "edit_file" not in profile.tools
    assert "bash" not in profile.tools
    assert "read_file" in profile.tools
    # System prompt must instruct read-only + JSON output.
    assert "modify" in profile.system.lower() and "json" in profile.system.lower()


def case_e002_tools_for_agent_readonly() -> None:
    from harness.agents.registry import get_agent_profile
    from harness.agents.runner import _tools_for_agent

    profile = get_agent_profile("evaluator")
    tools, handlers = _tools_for_agent(profile.tools)
    tool_names = {t.get("name") for t in tools}
    assert "write_file" not in tool_names and "edit_file" not in tool_names
    assert "bash" not in tool_names
    assert "read_file" in tool_names
    # Handlers map must not contain write/bash handlers.
    assert "write_file" not in handlers and "edit_file" not in handlers
    assert "bash" not in handlers


# --- E003: input assembly (incl. untracked files) ----------------------------

def case_e003_collect_inputs() -> None:
    from harness.evaluation import collect_inputs
    from harness.features import create_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        subprocess.run(["git", "init", "-q", str(ws)], check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(ws), check=False)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=False)
        (ws / "base.py").write_text("x = 1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(ws), check=False)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(ws), check=False)

        feat = create_feature(
            "分页修复",
            "list_users 全部分页无遗漏",
            "python smoke_ok.py",
            workspace=ws,
        )
        (ws / "smoke_ok.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
        verify_feature_command(feat.id, workspace=ws)
        # NEW untracked file — evaluator must see it.
        (ws / "new_feature.py").write_text("def list_users(): pass", encoding="utf-8")

        inputs = collect_inputs(feat.id, workspace=ws)
        text = inputs.to_text()
        assert feat.behavior in text
        assert feat.verification in text
        assert "exit_code: 0" in text  # evidence rendered
        assert "评分标准" in text  # rubric rendered
        # Untracked file content must be visible to the evaluator.
        assert "new_feature.py" in text
        assert "def list_users(): pass" in text
    finally:
        tmp.cleanup()


# --- E004 / E005: parsing ----------------------------------------------------

def case_e004_parse_wellformed() -> None:
    from harness.evaluation import parse_findings

    raw = json.dumps(
        {
            "passed": False,
            "summary": "缺少空查询处理",
            "findings": [
                {"issue": "空查询应返回空列表", "severity": "high", "evidence": "search.py:12"}
            ],
        }
    )
    parsed = parse_findings(raw)
    assert parsed.passed is False
    assert parsed.summary == "缺少空查询处理"
    assert len(parsed.items) == 1
    assert parsed.items[0]["severity"] == "high"
    assert parsed.error is None


def case_e005_parse_tolerant() -> None:
    from harness.evaluation import parse_findings

    # Wrapped in prose + code fence.
    wrapped = '评估如下：\n```json\n{"passed": true, "summary": "OK", "findings": []}\n```\n完'
    assert parse_findings(wrapped).passed is True

    # Garbage -> passed None, no raise.
    assert parse_findings("完全不是 JSON").passed is None
    assert parse_findings("").passed is None
    assert parse_findings('{"passed": "yes"}').passed is None  # wrong type
    assert parse_findings('[1,2,3]').passed is None  # not an object


# --- E006: record + cwd ------------------------------------------------------

def case_e006_record_evaluation() -> None:
    from harness.evaluation import run_evaluation
    from harness.features import create_feature, get_feature

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)
        captured: dict = {}

        def _fake_run_agent_task(description, prompt, agent_type, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return json.dumps({"passed": True, "summary": "符合需求", "findings": []})

        with (
            mock.patch("harness.evaluation.runner.run_agent_task", side_effect=_fake_run_agent_task),
            mock.patch(
                "harness.evaluation.runner.validate_agent_model", return_value=None
            ),
        ):
            updated = run_evaluation(feat.id, workspace=ws)

        assert updated.evaluation is not None
        assert updated.evaluation["passed"] is True
        assert updated.evaluation["findings"] == []
        # State must remain untouched (advisory only).
        assert updated.state == "not_started"
        # The subagent must run against the feature's workspace, not the
        # process's startup WORKDIR.
        assert captured.get("cwd") == str(ws.resolve())
        # Durable on disk.
        assert get_feature(feat.id, workspace=ws).evaluation["passed"] is True
    finally:
        tmp.cleanup()


# --- E007: explicit evaluation_required flag ---------------------------------

def case_e007_requires_evaluation_flag() -> None:
    from harness.evaluation import requires_evaluation
    from harness.features import create_feature

    tmp, ws = _tmp_workspace()
    try:
        opt_in = create_feature(
            "y", "b", "python scripts/check.py", workspace=ws, evaluation_required=True
        )
        skip = create_feature("x", "b", "pytest -q", workspace=ws)
        assert requires_evaluation(opt_in)
        # Even a pytest-verified feature skips unless explicitly opted in —
        # no implicit heuristic that hides scope/behavior issues.
        assert not requires_evaluation(skip)
    finally:
        tmp.cleanup()


CASES = [
    EvalCase(
        "e001.evaluator_registered_readonly",
        "E001: evaluator agent registered, read-only tools, no bash",
        "evaluation",
        case_e001_evaluator_registered_readonly,
    ),
    EvalCase(
        "e002.tools_readonly",
        "E002: evaluator tool pool resolves read-only handlers, no bash",
        "evaluation",
        case_e002_tools_for_agent_readonly,
    ),
    EvalCase(
        "e003.inputs_assembled",
        "E003: evaluation inputs include behavior/verification/evidence/diff + untracked",
        "evaluation",
        case_e003_collect_inputs,
    ),
    EvalCase(
        "e004.parse_wellformed",
        "E004: findings JSON parses correctly",
        "evaluation",
        case_e004_parse_wellformed,
    ),
    EvalCase(
        "e005.parse_tolerant",
        "E005: findings parsing tolerates malformed output",
        "evaluation",
        case_e005_parse_tolerant,
    ),
    EvalCase(
        "e006.record_advisory",
        "E006: evaluation recorded on feature, state untouched, cwd=feature workspace",
        "evaluation",
        case_e006_record_evaluation,
    ),
    EvalCase(
        "e007.requires_evaluation_flag",
        "E007: requires_evaluation is the explicit evaluation_required flag",
        "evaluation",
        case_e007_requires_evaluation_flag,
    ),
]
