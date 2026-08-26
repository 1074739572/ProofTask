"""Regression coverage for evaluator diagnostics and input prioritization."""

from __future__ import annotations

from types import SimpleNamespace

from harness.evaluation.inputs import EvaluationInputs, _current_task_evidence
from harness.evaluation.parser import parse_findings
from harness.evaluation.runner import build_evaluation_prompt
from harness.goal.models import GoalState
from harness.goal.memory import write_handoff
from harness.goal.prompt import build_goal_act_prompt


def test_evaluator_rejects_contradictory_passing_verdicts():
    parsed = parse_findings(
        '{"passed":true,"route":"blocked","summary":"ok","findings":[]}'
    )
    assert parsed.passed is None
    assert "inconsistent" in str(parsed.error)


def test_evaluator_includes_bound_test_source_before_large_diff():
    feature = SimpleNamespace(
        behavior="behavior",
        acceptance_cases=[],
        verification_spec={},
        verification="python -m pytest -q",
        evidence=[],
        start_snapshot=None,
        start_diff=None,
    )
    rendered = EvaluationInputs(
        feature=feature,
        diff="x" * 30_000,
        bound_test_sources=[("tests/test_bound.py", "def test_bound(): pass")],
    ).to_text()
    assert "tests/test_bound.py" in rendered
    assert "def test_bound(): pass" in rendered


def test_cross_task_impact_context_is_explicit_in_worker_and_evaluator_prompts(tmp_path):
    task = SimpleNamespace(
        id="task_target",
        subject="approval flow",
        description="keep remembered approvals working",
        acceptance_cases=[],
        verification_state="needs_generation",
        verification_spec={
            "impact_context": [{
                "source_task_id": "task_upstream",
                "reason": "the shared permission gate changed",
                "required_coverage": "cover approval persistence through the gate",
            }],
        },
        evidence=[],
        last_error=None,
        repair_history=[],
        behavior="keep remembered approvals working",
        verification="pytest -q",
        start_snapshot=None,
        start_diff=None,
    )
    state = GoalState.new(target="preserve approvals", verification="pytest -q", workspace=str(tmp_path))

    worker_prompt = build_goal_act_prompt(state, task)
    evaluator_prompt = build_evaluation_prompt(EvaluationInputs(feature=task, diff=""))

    assert "Cross-Task impact context" in worker_prompt
    assert "Do not regress the upstream behavior" in worker_prompt
    assert "Cross-Task impact context" in evaluator_prompt
    assert "missing required interaction coverage is never passable" in evaluator_prompt


def test_chinese_goal_worker_prompt_requests_chinese_human_summaries(tmp_path):
    task = SimpleNamespace(
        id="task_language", subject="优化输入", description="改善输入体验", acceptance_cases=[],
        verification_state="needs_generation", verification_spec={}, evidence=[], last_error=None,
        repair_history=[], behavior="改善输入体验", verification="pytest -q", start_snapshot=None,
        start_diff=None,
    )
    state = GoalState.new(target="优化输入", verification="pytest -q", workspace=str(tmp_path))
    state.goal_contract = {"language": "zh-CN"}

    prompt = build_goal_act_prompt(state, task)

    assert "Simplified Chinese" in prompt


def test_goal_worker_prompt_rehydrates_active_write_scope_and_tool_checkpoint(tmp_path):
    task = SimpleNamespace(
        id="task_checkpoint", subject="policy", description="write the policy", acceptance_cases=[],
        verification_state="not_started", verification_spec={}, evidence=[], last_error=None,
        repair_history=[], primary_write=["config/permissions.json"],
        planned_new=["harness/permission_policy.py"], conditional_write=[], read_envelope=[],
        behavior="write the policy",
    )
    state = GoalState.new(target="policy", verification="pytest -q", workspace=str(tmp_path))
    state.execution_workspace = str(tmp_path)
    write_handoff(
        state,
        task,
        phase="verify",
        execution={
            "task_id": task.id,
            "stop_reason": "max_rounds",
            "write_paths": ["config/permissions.json"],
            "write_outcomes": [],
            "tool_errors": ["write_file: Error: simulated write failure"],
        },
    )

    prompt = build_goal_act_prompt(state, task)

    assert f"Execution workspace root (tool cwd): {tmp_path.resolve()}" in prompt
    assert "config/permissions.json ->" in prompt
    assert "Do not request them again" in prompt
    assert "Previous worker execution checkpoint" in prompt
    assert "simulated write failure" in prompt


def test_task_scoped_diff_excludes_unchanged_preexisting_dirty_files(monkeypatch, tmp_path):
    import harness.evaluation.inputs as inputs_mod

    task = SimpleNamespace(start_dirty_hashes={"unrelated.py": "same", "edited.py": "before"})
    monkeypatch.setattr(
        "harness.verification.snapshot.capture_dirty_file_hashes",
        lambda _workspace: {"unrelated.py": "same", "edited.py": "after", "new.py": "new"},
    )
    requested = []
    monkeypatch.setattr(
        inputs_mod,
        "_git_diff",
        lambda _workspace, paths=None: requested.append(paths) or "task diff",
    )

    assert inputs_mod._task_scoped_diff(task, tmp_path) == "task diff"
    assert requested == [{"edited.py", "new.py"}]


def test_task_scoped_diff_honors_declared_scope_paths(monkeypatch, tmp_path):
    import harness.evaluation.inputs as inputs_mod

    task = SimpleNamespace(
        start_dirty_hashes={},
        scope_paths=["node_tui/src-open/App.tsx", "node_tui/src-open/interaction.ts"],
    )
    monkeypatch.setattr(
        "harness.verification.snapshot.capture_dirty_file_hashes",
        lambda _workspace: {
            "node_tui/src-open/App.tsx": "changed",
            "node_tui/src-open/interaction.ts": "changed",
            "harness/goal/runner.py": "unrelated",
            ".env.bak_20260818": "secret",
        },
    )
    requested = []
    monkeypatch.setattr(
        inputs_mod,
        "_git_diff",
        lambda _workspace, paths=None: requested.append(paths) or "task diff",
    )

    assert inputs_mod._task_scoped_diff(task, tmp_path) == "task diff"
    assert requested == [{"node_tui/src-open/App.tsx", "node_tui/src-open/interaction.ts"}]


def test_task_scoped_diff_renders_only_later_changes_to_restored_dirty_file(monkeypatch, tmp_path):
    import harness.evaluation.inputs as inputs_mod

    source = tmp_path / "harness" / "goal" / "sandbox.py"
    source.parent.mkdir(parents=True)
    source.write_text("restored = True\ncurrent = True\n", encoding="utf-8")
    task = SimpleNamespace(
        start_dirty_hashes={"harness/goal/sandbox.py": "before"},
        start_dirty_contents={"harness/goal/sandbox.py": "restored = True\n"},
        scope_paths=["harness/goal/sandbox.py"],
    )
    monkeypatch.setattr(
        "harness.verification.snapshot.capture_dirty_file_hashes",
        lambda _workspace: {"harness/goal/sandbox.py": "after"},
    )
    monkeypatch.setattr(inputs_mod, "_git_diff", lambda *_args, **_kwargs: "")

    rendered = inputs_mod._task_scoped_diff(task, tmp_path)

    assert "restored = True" not in rendered
    assert "+current = True" in rendered


def test_untracked_credential_backups_are_not_rendered(monkeypatch, tmp_path):
    import harness.evaluation.inputs as inputs_mod

    class Result:
        returncode = 0
        stdout = "?? .env.bak_20260818\n?? notes.md\n"

    def run(command, **kwargs):
        if command[1] == "status":
            return Result()
        return type("DiffResult", (), {"returncode": 0, "stdout": "tracked diff"})()

    monkeypatch.setattr(inputs_mod.subprocess, "run", run)
    rendered = inputs_mod._git_diff(tmp_path)

    assert "tracked diff" in rendered
    assert ".env.bak_20260818" not in rendered


def test_evaluator_input_explains_that_a_clean_diff_is_not_a_scope_failure():
    feature = SimpleNamespace(
        behavior="behavior",
        acceptance_cases=[],
        verification_spec={},
        verification="pytest -q",
        evidence=[],
        start_snapshot=None,
        start_diff=None,
    )

    rendered = EvaluationInputs(feature=feature, diff="").to_text()

    assert "clean diff can mean the Task work was committed" in rendered
    assert "Do not fail scope merely" in rendered


def test_evaluator_input_does_not_expose_full_pre_task_diff_as_current_work():
    feature = SimpleNamespace(
        behavior="parse configuration only",
        acceptance_cases=[],
        verification_spec={},
        verification="pytest -q",
        evidence=[],
        start_snapshot="abc:123",
        start_diff="diff --git a/harness/goal/runner.py b/harness/goal/runner.py\n+Docker integration",
        start_dirty_hashes={"harness/goal/runner.py": "before"},
    )

    rendered = EvaluationInputs(feature=feature, diff="sandbox-only delta").to_text()

    assert "harness/goal/runner.py" in rendered
    assert "Docker integration" not in rendered
    assert "historical diff is intentionally omitted" in rendered


def test_task_evaluator_uses_latest_matching_bound_verification_only():
    task = SimpleNamespace(
        verification_spec={"command": "python -m pytest -q tests/test_bound.py"},
        evidence=[
            {
                "command": "python -m pytest -q tests/test_bound.py",
                "exit_code": 1,
                "stdout_tail": "old failure",
            },
            {
                "command": "python -m pytest -q tests/test_bound.py",
                "exit_code": 0,
                "stdout_tail": "1 passed",
            },
        ],
    )

    effective = _current_task_evidence(task)
    rendered = EvaluationInputs(
        feature=SimpleNamespace(
            behavior="behavior",
            acceptance_cases=[],
            verification_spec=task.verification_spec,
            verification="pytest -q",
            evidence=task.evidence,
            start_snapshot=None,
            start_diff=None,
        ),
        diff="",
        effective_evidence=effective,
    ).to_text()

    assert effective == [task.evidence[-1]]
    assert "1 passed" in rendered
    assert "old failure" not in rendered
    assert "retained for audit" in rendered


def test_evaluator_input_renders_all_bound_selectors_and_write_scope():
    selectors = [f"tests/test_bound.py::test_case_{index}" for index in range(10)]
    task = SimpleNamespace(
        behavior="behavior",
        acceptance_cases=[],
        verification_spec={},
        verification="pytest -q",
        evidence=[{"selectors": selectors, "exit_code": 0}],
        primary_write=["harness/goal/sandbox.py"],
        planned_new=[],
        conditional_write=[],
        start_snapshot=None,
        start_diff=None,
    )

    rendered = EvaluationInputs(feature=task, diff="").to_text()

    assert "Approved implementation scope" in rendered
    assert "harness/goal/sandbox.py" in rendered
    assert selectors[-1] in rendered


def test_scope_replan_is_required_when_evaluator_names_unapproved_source_file():
    from harness.evaluation.runner import _requires_scope_replan

    task = SimpleNamespace(
        primary_write=["harness/goal/sandbox.py"],
        planned_new=[],
        conditional_write=[],
    )
    payload = {
        "findings": [{
            "issue": "runner integration remains mutable",
            "evidence": "harness/goal/runner.py stores a mutable dict",
        }],
    }

    assert _requires_scope_replan(task, payload) is True


def test_scope_replan_ignores_test_paths_named_in_evaluator_evidence():
    from harness.evaluation.runner import _requires_scope_replan

    task = SimpleNamespace(
        primary_write=["harness/goal/sandbox.py"],
        planned_new=[],
        conditional_write=[],
    )
    payload = {
        "findings": [{
            "issue": "coverage is incomplete",
            "evidence": "tests/test_goal_sandbox_config_snapshot.py omits a fractional-limit case",
        }],
    }

    assert _requires_scope_replan(task, payload) is False
