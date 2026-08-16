"""Durable, bounded context handoff for autonomous Goal workers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.goal.store import project_dir


def _root(state) -> Path:
    return project_dir(state.workspace) / "goal-memory" / state.id


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def append_decisions(state, task, decisions: list[dict[str, str]], *, source: str) -> None:
    """Append structured decisions; Markdown is derived for human inspection."""
    if not decisions:
        return
    root = _root(state)
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "at": time.time(),
        "source": source,
        "task_id": task.id if task is not None else None,
        "decisions": decisions,
    }
    with (root / "decisions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    markdown = root / "decision-log.md"
    with markdown.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} | {source}\n")
        if task is not None:
            handle.write(f"Task: `{task.id}` {task.subject}\n\n")
        for item in decisions:
            handle.write(f"- Decision: {item.get('decision', '')}\n  Basis: {item.get('basis', '')}\n")


def recent_decisions(state, *, limit: int = 12) -> list[dict[str, Any]]:
    path = _root(state) / "decisions.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in rows if isinstance(row, dict)][-limit:]


def write_handoff(state, task, *, phase: str, summary: str = "") -> None:
    """Snapshot the exact facts a fresh internal worker needs, never chat logs."""
    payload = {
        "goal_id": state.id,
        "phase": phase,
        "target": state.target,
        "goal_contract": state.goal_contract,
        "task": {
            "id": task.id,
            "subject": task.subject,
            "behavior": task.description,
            "acceptance_cases": task.acceptance_cases,
            "verification_spec": task.verification_spec,
            "last_error": task.last_error,
            "repair_history": task.repair_history[-3:],
        },
        "decisions": recent_decisions(state),
        "summary": summary[:4_000],
        "updated_at": time.time(),
    }
    _atomic_json(_root(state) / "handoff.json", payload)


def load_test_map(state) -> list[dict[str, Any]]:
    data = _read_json(_root(state) / "test-map.json", [])
    return data if isinstance(data, list) else []


def record_test_binding(state, task, spec: dict[str, Any], *, kind: str = "task") -> None:
    """Versioned TestMap entry, keyed by selector and immutable file hashes."""
    entries = load_test_map(state)
    revision = 1 + max((int(item.get("revision") or 0) for item in entries), default=0)
    entries.append(
        {
            "binding_id": f"{task.id}:r{revision}",
            "revision": revision,
            "task_ids": list(spec.get("owners") or [task.id]),
            "selectors": list(spec.get("selectors") or []),
            "test_hashes": dict(spec.get("test_hashes") or {}),
            "covers": list(spec.get("covers") or []),
            "kind": kind,
            "baseline_evidence": dict(spec.get("baseline_evidence") or {}),
            "base_snapshot": task.start_snapshot,
            "created_at": time.time(),
        }
    )
    _atomic_json(_root(state) / "test-map.json", entries)


def remove_test_bindings(state, task_id: str) -> None:
    """Remove bindings owned only by a discarded synthetic Goal Task."""
    entries = [
        entry for entry in load_test_map(state)
        if task_id not in (entry.get("task_ids") or [])
    ]
    _atomic_json(_root(state) / "test-map.json", entries)
