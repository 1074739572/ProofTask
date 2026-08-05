"""Independent behavior check for H004 (task store, after session 2).

Verifies the FULL task API the two sessions were supposed to build:
add_task / list_tasks / complete_task / delete_task.

Usage: python h004_task_check.py <workspace_dir>
Exit 0 = pass; non-zero with messages = failure.
"""

import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from app import TaskStore  # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


store = TaskStore()

# add_task
t1 = store.add_task("write report")
check("add_task returns task with id", isinstance(t1.get("id"), int))
check("add_task stores title", t1["title"] == "write report")
check("add_task starts not done", t1.get("done") is False)

t2 = store.add_task("fix bug")
check("add_task increments id", t2["id"] != t1["id"])

# list_tasks
tasks = store.list_tasks()
check("list_tasks returns 2 tasks", len(tasks) == 2)
check("list_tasks preserves order", [t["title"] for t in tasks] == ["write report", "fix bug"])

# complete_task
store.complete_task(t1["id"])
tasks = store.list_tasks()
check("complete_task marks done", next(t for t in tasks if t["id"] == t1["id"])["done"] is True)
check("complete_task leaves other undone", next(t for t in tasks if t["id"] == t2["id"])["done"] is False)

# delete_task
store.delete_task(t2["id"])
tasks = store.list_tasks()
check("delete_task removes task", all(t["id"] != t2["id"] for t in tasks))
check("delete_task keeps other task", any(t["id"] == t1["id"] for t in tasks))

if failures:
    print("H004_CHECK_FAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("H004_CHECK_PASS: task store contract satisfied")
sys.exit(0)
