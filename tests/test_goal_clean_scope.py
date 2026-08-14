"""Task verification is the completion gate for Goal mode."""

from harness.tasks import claim_task, complete_task, create_task, load_task, set_task_verification_result


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
