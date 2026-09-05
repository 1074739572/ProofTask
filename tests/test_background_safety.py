import time
from unittest import mock


def test_background_notification_preserves_real_exit_code():
    from harness.agent import background

    with background.background_lock:
        background.background_tasks.clear()
        background.background_results.clear()
        background.background_tasks["bg_test"] = {
            "command": "pytest -q",
            "status": "completed",
            "tool_use_id": "t1",
        }
        background.background_results["bg_test"] = "[exit_code=7]\nfailed"
    notes = background.collect_background_results()
    assert "<exit_code>7</exit_code>" in notes[0]


def test_background_worker_skips_execution_after_cancel():
    from harness.agent import background

    block = {"name": "bash", "input": {"command": "echo unsafe"}, "id": "t2"}
    handlers = {"bash": mock.Mock(return_value="should not run")}
    with mock.patch("harness.agent.background.is_cancelled", return_value=True):
        task_id = background.start_background_task(block, handlers)
    # Worker is a daemon; poll briefly for the terminal state.
    task = None
    for _ in range(20):
        with background.background_lock:
            task = background.background_tasks.get(task_id)
        if task and task["status"] != "running":
            break
        time.sleep(0.01)
    assert task is not None
    assert task["status"] in {"running", "cancelled"}
    handlers["bash"].assert_not_called()
    with background.background_lock:
        background.background_tasks.pop(task_id, None)
        background.background_results.pop(task_id, None)
