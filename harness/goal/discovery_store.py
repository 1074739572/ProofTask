"""Atomic persistence for resumable Goal discovery jobs and manifests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from harness.goal.store import project_dir


def discovery_dir(workspace: str | Path, goal_id: str) -> Path:
    return project_dir(workspace) / "goal-memory" / goal_id / "discovery"


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def save_job_state(workspace: str | Path, goal_id: str, payload: dict[str, Any]) -> Path:
    path = discovery_dir(workspace, goal_id) / "jobs" / f"{payload['id']}.json"
    _atomic_json(path, payload)
    return path


def load_job_states(workspace: str | Path, goal_id: str) -> list[dict[str, Any]]:
    root = discovery_dir(workspace, goal_id) / "jobs"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")) if root.exists() else ():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def save_report(workspace: str | Path, goal_id: str, job_id: str, payload: dict[str, Any]) -> Path:
    path = discovery_dir(workspace, goal_id) / "reports" / f"{job_id}.json"
    _atomic_json(path, payload)
    return path


def save_manifest(workspace: str | Path, goal_id: str, payload: dict[str, Any]) -> Path:
    path = discovery_dir(workspace, goal_id) / "manifest.json"
    _atomic_json(path, payload)
    return path


def load_manifest(workspace: str | Path, goal_id: str) -> dict[str, Any] | None:
    path = discovery_dir(workspace, goal_id) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
