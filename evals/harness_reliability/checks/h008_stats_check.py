"""Hidden oracle for task_store3 session-3 stats feature (H008).

Verifies behavior contract only (no naming/structure coupling):
- stats() returns total/completed/pending counts consistent with the store
- after add/completed/delete, counts are correct
"""
import sys
from pathlib import Path

workspace = Path(sys.argv[1])
sys.path.insert(0, str(workspace))

from app import TaskStore  # noqa: E402


def main() -> int:
    s = TaskStore()
    s.add_task("a")
    s.add_task("b")
    s.add_task("c")

    st = s.stats()
    assert isinstance(st, dict), f"stats() must return a dict, got {type(st)}"
    assert st.get("total") == 3, f"total should be 3, got {st}"
    assert st.get("completed") == 0, f"completed should be 0, got {st}"
    assert st.get("pending") == 3, f"pending should be 3, got {st}"

    s.complete_task(s.list_tasks()[0]["id"])
    st = s.stats()
    assert st["total"] == 3 and st["completed"] == 1 and st["pending"] == 2, f"after complete: {st}"

    s.delete_task(s.list_tasks()[1]["id"])
    st = s.stats()
    assert st["total"] == 2 and st["completed"] == 1 and st["pending"] == 1, f"after delete: {st}"

    print("H008 stats checks PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"H008 FAIL: {e}")
        raise SystemExit(1)
