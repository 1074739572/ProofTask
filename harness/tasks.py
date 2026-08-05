"""Durable task graph with dependencies, ownership, and archive lifecycle.

Active board: `.tasks/task_*.json` (pending / in_progress only).
Completed tasks move to `.tasks/archive/` so list_tasks and teammates
do not keep steering new work from old goals.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.settings import TASKS_DIR


def _archive_dir() -> Path:
    """Archive directory — computed at call time so tests that patch
    TASKS_DIR (or a workspace switch) never write to a stale path."""
    return TASKS_DIR / "archive"


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
    # L2 extensions — defaulted so legacy JSON files (without these keys)
    # still load via Task(**data).
    feature_ids: list[str] = field(default_factory=list)
    attempts: int = 0
    last_error: str | None = None


def _active_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _archive_path(task_id: str) -> Path:
    return _archive_dir() / f"{task_id}.json"


def _find_task_path(task_id: str) -> Path | None:
    active = _active_path(task_id)
    if active.exists():
        return active
    archived = _archive_path(task_id)
    if archived.exists():
        return archived
    return None


def _load_task_from_path(path: Path) -> Task:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if "completed_at" not in data:
        data["completed_at"] = None
    return Task(**data)


def create_task(
    subject: str,
    description: str = "",
    blockedBy: list[str] | None = None,
) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
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
    """Active board only by default — completed tasks live under archive/."""
    tasks = [
        _load_task_from_path(path)
        for path in sorted(TASKS_DIR.glob("task_*.json"))
    ]
    if include_archived and _archive_dir().exists():
        tasks.extend(
            _load_task_from_path(path)
            for path in sorted(_archive_dir().glob("task_*.json"))
        )
    return tasks


def list_archived_tasks() -> list[Task]:
    if not _archive_dir().exists():
        return []
    return [
        _load_task_from_path(path)
        for path in sorted(_archive_dir().glob("task_*.json"))
    ]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def _dependency_satisfied(dep_id: str) -> bool:
    path = _find_task_path(dep_id)
    if not path:
        return False
    return _load_task_from_path(path).status == "completed"


def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    return all(_dependency_satisfied(dep_id) for dep_id in task.blockedBy)


def claim_task(task_id: str, owner: str = "agent") -> str:
    path = _active_path(task_id)
    if not path.exists():
        task = load_task(task_id)
        if task.status == "completed":
            return f"Task {task_id} is completed and archived; create a new task instead"
        return f"Task {task_id} is not on the active board"
    task = _load_task_from_path(path)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        deps = [
            dep
            for dep in task.blockedBy
            if not _dependency_satisfied(dep)
        ]
        missing = [
            dep for dep in task.blockedBy if _find_task_path(dep) is None
        ]
        parts = []
        if deps:
            parts.append(f"blocked by: {deps}")
        if missing:
            parts.append(f"missing deps: {missing}")
        return "Cannot start — " + ", ".join(parts)
    task.owner = owner
    task.status = "in_progress"
    task.attempts += 1
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def attach_feature(task_id: str, feature_id: str) -> str:
    """Link a feature (harness.features) to a task. Idempotent."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"task {task_id} is not on the active board")
    task = _load_task_from_path(path)
    if feature_id not in task.feature_ids:
        task.feature_ids.append(feature_id)
        save_task(task)
        return f"Attached {feature_id} to {task_id}"
    return f"{feature_id} already attached to {task_id}"


def _archive_task(task: Task) -> None:
    _archive_dir().mkdir(parents=True, exist_ok=True)
    save_task(task, archived=True)
    active = _active_path(task.id)
    if active.exists():
        active.unlink()


def complete_task(task_id: str) -> str:
    path = _active_path(task_id)
    if not path.exists():
        task = load_task(task_id)
        if task.status == "completed":
            return f"Task {task_id} already completed (archived)"
        return f"Task {task_id} is not on the active board"
    task = _load_task_from_path(path)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"

    # L4 clean-state gate: report in warn mode; block in enforce mode.
    # (Delayed import keeps harness.tasks free of a hard dependency on clean.)
    from harness.clean import clean_mode, run_clean_check
    from harness.settings import get_workspace_paths
    from harness.ui import events

    mode = clean_mode()

    # Check the task's own workspace (its worktree, if bound) — not whatever
    # workspace the process happens to be in right now.
    ws_paths = get_workspace_paths()
    check_ws: Path | None = None
    if task.worktree:
        candidate = ws_paths.worktrees_dir / task.worktree
        if candidate.exists():
            check_ws = candidate

    report = run_clean_check(check_ws)
    if mode == "enforce" and not report.ok:
        failures = ", ".join(c.id for c in report.hard_failures)
        if not events.is_enabled():
            print(f"\n\033[31m[clean:enforce] blocking completion — {failures}\033[0m")
            print(report.summary())
        return (
            f"Cannot complete {task.id} — clean-state check failed ({failures}). "
            f"Fix the issues and retry. Details:\n{report.summary()}"
        )
    if mode == "warn" and not report.ok and not events.is_enabled():
        print(f"\n\033[33m[clean:warn]\033[0m")
        print(report.summary())

    # L2 completion gate: every linked feature must be passing and fresh.
    if task.feature_ids:
        from harness.features import feature_is_stale, list_features

        features = list_features(workspace=check_ws) if check_ws else list_features()
        by_id = {f.id: f for f in features}
        missing = [fid for fid in task.feature_ids if fid not in by_id]
        not_passing = [
            f"{fid} ({by_id[fid].name}) [{by_id[fid].state}]"
            for fid in task.feature_ids
            if fid in by_id and by_id[fid].state != "passing"
        ]
        stale = [
            f"{fid} ({by_id[fid].name})"
            for fid in task.feature_ids
            if fid in by_id and by_id[fid].state == "passing" and feature_is_stale(by_id[fid])
        ]
        problems: list[str] = []
        if missing:
            problems.append("missing features: " + ", ".join(missing))
        if not_passing:
            problems.append("not passing: " + ", ".join(not_passing))
        if stale:
            problems.append("stale (code changed after verification): " + ", ".join(stale))
        if problems:
            detail = "; ".join(problems)
            if mode == "enforce":
                if not events.is_enabled():
                    print(f"\n\033[31m[clean:enforce] blocking completion — features not done\033[0m")
                    print(f"  {detail}")
                return (
                    f"Cannot complete {task.id} — linked features are not all "
                    f"passing and fresh. {detail}"
                )
            if not events.is_enabled():
                print(f"\n\033[33m[clean:warn]\033[0m features not all done: {detail}")

    task.status = "completed"
    task.completed_at = time.time()
    _archive_task(task)
    unblocked = [
        item.subject
        for item in list_tasks()
        if item.status == "pending" and item.blockedBy and can_start(item.id)
    ]
    print(f"  \033[32m[complete] {task.subject} archived\033[0m")
    msg = f"Completed {task.id} ({task.subject}) — removed from active board"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


def reconcile_task_board() -> int:
    """Move legacy completed files still on the active board into archive/."""
    moved = 0
    for path in list(TASKS_DIR.glob("task_*.json")):
        task = _load_task_from_path(path)
        if task.status != "completed":
            continue
        if task.completed_at is None:
            task.completed_at = path.stat().st_mtime
        _archive_task(task)
        moved += 1
    return moved


def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for path in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        if (
            task.get("status") == "pending"
            and not task.get("owner")
            and can_start(task["id"])
        ):
            unclaimed.append(task)
    return unclaimed


def clear_active_tasks(*, archive: bool = True) -> str:
    """Remove all tasks from the active board (optional archive before delete)."""
    paths = list(TASKS_DIR.glob("task_*.json"))
    if not paths:
        return "Active task board is already empty."
    if archive:
        _archive_dir().mkdir(parents=True, exist_ok=True)
        for path in paths:
            task = _load_task_from_path(path)
            task.status = "cancelled"
            task.completed_at = time.time()
            save_task(task, archived=True)
            path.unlink()
        return f"Cleared {len(paths)} active task(s); archived under .tasks/archive/"
    for path in paths:
        path.unlink()
    return f"Deleted {len(paths)} active task(s)."
