"""Durable Task graph and Task-owned verification contracts.

Tasks are the only execution unit used by Goal mode. A Task may be worked on
through any number of todos, but only a zero-exit machine verification result
can make it eligible for completion.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.settings import TASKS_DIR

_ORIGINAL_TASKS_DIR = TASKS_DIR


def _tasks_dir() -> Path:
    if TASKS_DIR != _ORIGINAL_TASKS_DIR:
        return TASKS_DIR
    from harness.settings import get_workspace_paths

    return get_workspace_paths().tasks_dir


def _archive_dir() -> Path:
    return _tasks_dir() / "archive"


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None
    completed_at: float | None = None
    acceptance_cases: list[dict] = field(default_factory=list)
    verification_spec: dict = field(default_factory=dict)
    verification_state: str = "not_started"
    evidence: list[dict] = field(default_factory=list)
    evaluation: dict | None = None
    evaluation_required: bool = False
    attempts: int = 0
    last_error: str | None = None
    goal_id: str | None = None

    @property
    def name(self) -> str:
        return self.subject

    @property
    def behavior(self) -> str:
        return self.description

    @property
    def verification(self) -> str:
        return str(self.verification_spec.get("command") or "")


def _active_path(task_id: str) -> Path:
    return _tasks_dir() / f"{task_id}.json"


def _archive_path(task_id: str) -> Path:
    return _archive_dir() / f"{task_id}.json"


def _find_task_path(task_id: str) -> Path | None:
    active = _active_path(task_id)
    if active.exists():
        return active
    archived = _archive_path(task_id)
    return archived if archived.exists() else None


def _load_task_from_path(path: Path) -> Task:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    allowed = set(Task.__dataclass_fields__)
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unsupported task fields: {sorted(unknown)}")
    return Task(**data)


def create_task(
    subject: str,
    description: str = "",
    blockedBy: list[str] | None = None,
    *,
    goal_id: str | None = None,
    acceptance_cases: list[dict] | None = None,
    verification_spec: dict | None = None,
    evaluation_required: bool = False,
) -> Task:
    spec = dict(verification_spec or {})
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=list(blockedBy or []),
        goal_id=goal_id,
        acceptance_cases=list(acceptance_cases or []),
        verification_spec=spec,
        verification_state=("needs_generation" if spec.get("source") == "needs_generation" else "not_started"),
        evaluation_required=evaluation_required,
    )
    save_task(task)
    return task


def save_task(task: Task, *, archived: bool = False) -> None:
    path = _archive_path(task.id) if archived else _active_path(task.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")


def load_task(task_id: str) -> Task:
    path = _find_task_path(task_id)
    if not path:
        raise FileNotFoundError(task_id)
    return _load_task_from_path(path)


def list_tasks(*, include_archived: bool = False) -> list[Task]:
    tasks = [_load_task_from_path(path) for path in sorted(_tasks_dir().glob("task_*.json"))]
    if include_archived and _archive_dir().exists():
        tasks.extend(_load_task_from_path(path) for path in sorted(_archive_dir().glob("task_*.json")))
    return tasks


def list_archived_tasks() -> list[Task]:
    if not _archive_dir().exists():
        return []
    return [_load_task_from_path(path) for path in sorted(_archive_dir().glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def _dependency_satisfied(dep_id: str) -> bool:
    try:
        return load_task(dep_id).status == "completed"
    except FileNotFoundError:
        return False


def can_start(task_id: str) -> bool:
    return all(_dependency_satisfied(dep_id) for dep_id in load_task(task_id).blockedBy)


def claim_task(task_id: str, owner: str = "agent") -> str:
    path = _active_path(task_id)
    if not path.exists():
        task = load_task(task_id)
        return f"Task {task_id} is completed and archived; create a new task instead" if task.status == "completed" else f"Task {task_id} is not on the active board"
    task = _load_task_from_path(path)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        return f"Cannot start task {task_id}: dependencies are incomplete"
    task.owner = owner
    task.status = "in_progress"
    task.attempts += 1
    save_task(task)
    return f"Claimed {task.id} ({task.subject})"


def _archive_task(task: Task) -> None:
    _archive_dir().mkdir(parents=True, exist_ok=True)
    save_task(task, archived=True)
    active = _active_path(task.id)
    if active.exists():
        active.unlink()


def set_task_verification_result(
    task_id: str,
    *,
    passed: bool,
    evidence: dict | None = None,
    error: str | None = None,
) -> Task:
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    if task.status != "in_progress":
        raise ValueError(f"cannot verify task in state {task.status!r}")
    if passed:
        if not evidence or evidence.get("exit_code") != 0:
            raise ValueError("passing verification requires zero-exit evidence")
        task.evidence.append(dict(evidence))
        task.verification_state = "passing"
        task.last_error = None
    else:
        if evidence:
            task.evidence.append(dict(evidence))
        task.verification_state = "failing"
        task.last_error = error or "verification failed"
    save_task(task)
    return task


def record_task_evaluation(task_id: str, evaluation: dict) -> Task:
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.evaluation = dict(evaluation)
    save_task(task)
    return task


def record_task_reverification(
    task_id: str,
    *,
    passed: bool,
    evidence: dict | None = None,
    error: str | None = None,
) -> Task:
    """Append final-goal verification evidence to an active or archived Task."""
    path = _find_task_path(task_id)
    if path is None:
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    if evidence:
        task.evidence.append(dict(evidence))
    task.verification_state = "passing" if passed else "failing"
    task.last_error = None if passed else (error or "final task verification failed")
    save_task(task, archived=path.parent == _archive_dir())
    return task


def bind_task_verification(task_id: str, verification_spec: dict) -> Task:
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.verification_spec = dict(verification_spec)
    task.verification_state = "not_started"
    task.last_error = None
    save_task(task)
    return task


def complete_task(task_id: str, *, clean_check_mode: str | None = None) -> str:
    path = _active_path(task_id)
    if not path.exists():
        task = load_task(task_id)
        return f"Task {task_id} already completed (archived)" if task.status == "completed" else f"Task {task_id} is not on the active board"
    task = _load_task_from_path(path)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.verification_spec and task.verification_state != "passing":
        return f"Cannot complete {task.id}: bound verification has not passed ({task.verification_state})"

    from harness.clean import clean_mode, run_clean_check
    from harness.settings import get_workspace_paths

    check_ws: Path | None = None
    if task.worktree:
        candidate = get_workspace_paths().worktrees_dir / task.worktree
        if candidate.exists():
            check_ws = candidate
    mode = clean_check_mode or clean_mode()
    report = run_clean_check(check_ws, mode=mode, include_feature_consistency=False)
    if mode == "enforce" and not report.ok:
        return f"Cannot complete {task.id}: clean-state check failed. {report.summary()}"

    task.status = "completed"
    task.completed_at = time.time()
    _archive_task(task)
    return f"Completed {task.id} ({task.subject})"


def reconcile_task_board() -> int:
    moved = 0
    for path in list(_tasks_dir().glob("task_*.json")):
        task = _load_task_from_path(path)
        if task.status == "completed":
            task.completed_at = task.completed_at or path.stat().st_mtime
            _archive_task(task)
            moved += 1
    return moved


def scan_unclaimed_tasks() -> list[dict]:
    return [asdict(task) for task in list_tasks() if task.status == "pending" and not task.owner and can_start(task.id)]


def clear_active_tasks(*, archive: bool = True) -> str:
    paths = list(_tasks_dir().glob("task_*.json"))
    if not paths:
        return "Active task board is already empty."
    for path in paths:
        task = _load_task_from_path(path)
        if archive:
            task.status = "cancelled"
            task.completed_at = time.time()
            save_task(task, archived=True)
        path.unlink()
    return f"Cleared {len(paths)} active task(s)" if archive else f"Deleted {len(paths)} active task(s)"
