"""Task list demo — skeleton for a two-session eval task.

Session 1 (prompt): implement add_task() + list_tasks().
Session 2 (prompt): implement complete_task() + delete_task(), add tests.

The fixture ships as an empty skeleton with one passing test so the
project is runnable from the start.
"""


class TaskStore:
    def __init__(self):
        self._tasks = {}   # id -> {"id": int, "title": str, "done": bool}
        self._next_id = 1

    # --- session 1: to be implemented by the agent ---
    def add_task(self, title):
        raise NotImplementedError

    def list_tasks(self):
        raise NotImplementedError

    # --- session 2: to be implemented by the agent ---
    def complete_task(self, task_id):
        raise NotImplementedError

    def delete_task(self, task_id):
        raise NotImplementedError
