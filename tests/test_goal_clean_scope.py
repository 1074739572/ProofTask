"""Task verification is the completion gate for Goal mode."""

from harness.tasks import claim_task, complete_task, create_task, load_task, record_task_evaluation, set_task_verification_result


def test_task_cannot_complete_without_passing_bound_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CLEAN_MODE", "off")
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = create_task("x", "behavior", verification_spec={"command": "pytest -q", "selectors": ["tests/test_x.py::test_x"], "collected_count": 1, "source": "generated"})
    claim_task(task.id)
    assert "has not passed" in complete_task(task.id)
    set_task_verification_result(task.id, passed=True, evidence={"command": "pytest -q", "exit_code": 0})
    assert complete_task(task.id).startswith("Completed")
    assert load_task(task.id).status == "completed"


def test_task_feature_links_survive_durable_reload(tmp_path, monkeypatch):
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = create_task("x", "behavior", feature_ids=["feat_rate_limit"])

    assert load_task(task.id).feature_ids == ["feat_rate_limit"]


def test_goal_task_requires_passing_eval_and_coverage_for_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CLEAN_MODE", "off")
    import harness.tasks as tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path / ".tasks")
    task = create_task(
        "x",
        "behavior",
        goal_id="goal_x",
        acceptance_cases=[{"id": "AC1", "given": "x", "when": "y", "then": "z"}],
        verification_spec={
            "command": "pytest -q",
            "selectors": ["tests/test_x.py::test_x"],
            "collected_count": 1,
                "source": "generated",
                "covers": ["AC1"],
                "case_selectors": {"AC1": ["tests/test_x.py::test_x"]},
            },
        evaluation_required=True,
    )
    claim_task(task.id)
    set_task_verification_result(task.id, passed=True, evidence={"command": "pytest -q", "exit_code": 0})
    assert "evaluation has not passed" in complete_task(task.id)
    record_task_evaluation(task.id, {"passed": True})
    assert complete_task(task.id).startswith("Completed")
