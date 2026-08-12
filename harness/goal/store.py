"""Atomic goal persistence (L6 /goal).

Storage layout (per workspace)::

    <workspace>/.project/goal.json                  current goal slot
    <workspace>/.project/goal-history/<goal_id>.json  archived terminal copies

Rules:

- every write is atomic (temp file + ``os.replace``), so a crash mid-write
  never corrupts ``goal.json``;
- ``load_goal`` never overwrites a corrupt file — it raises
  :class:`GoalStoreError` and the caller reports it;
- a ``running``/``pausing`` state found on disk after a process restart is
  normalized in-memory to ``paused`` (``stop_reason=process_restarted``) —
  the goal must be explicitly resumed;
- paths are computed from ``get_workspace_paths()`` at call time so ``/open``
  switches read the new workspace's goal.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.goal.models import (
    GOAL_SCHEMA_VERSION,
    GoalPhase,
    GoalStatus,
    GoalState,
    StopReason,
)
from harness.settings import get_workspace_paths

GOAL_FILENAME = "goal.json"
HISTORY_DIRNAME = "goal-history"


class GoalStoreError(Exception):
    """Raised for corrupt / unsupported goal state files.

    ``code`` is a stable machine-readable tag: ``goal_state_corrupt``,
    ``unsupported_schema``.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def project_dir(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        return Path(workspace).expanduser().resolve() / ".project"
    return get_workspace_paths().project_dir


def goal_path(workspace: str | Path | None = None) -> Path:
    return project_dir(workspace) / GOAL_FILENAME


def history_dir(workspace: str | Path | None = None) -> Path:
    return project_dir(workspace) / HISTORY_DIRNAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_goal(state: GoalState) -> None:
    state.updated_at = time.time()
    _atomic_write(goal_path(state.workspace or None), state.to_dict())


def load_goal(workspace: str | Path | None = None) -> GoalState | None:
    """Load the current goal slot, or None when absent.

    Corrupt / unsupported files raise :class:`GoalStoreError` and are never
    overwritten here. A ``running``/``pausing`` goal left by a previous process
    is normalized in-memory to ``paused`` (``stop_reason=process_restarted``).
    """
    path = goal_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalStoreError("goal_state_corrupt", f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GoalStoreError("goal_state_corrupt", f"{path} is not a JSON object")
    if data.get("schema_version") != GOAL_SCHEMA_VERSION:
        raise GoalStoreError(
            "unsupported_schema",
            f"{path} uses schema_version={data.get('schema_version')!r}, "
            f"expected {GOAL_SCHEMA_VERSION}",
        )
    try:
        state = GoalState.from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise GoalStoreError("goal_state_corrupt", f"invalid goal state in {path}: {exc}") from exc

    # A durable cancellation request is final even if the process exited before
    # the worker reached its next checkpoint.
    if state.status == GoalStatus.CANCELLING.value:
        previous = state.phase
        state.last_phase = previous
        state.status = GoalStatus.CANCELLED.value
        state.phase = GoalPhase.CANCELLED.value
        state.completed_at = state.completed_at or time.time()
        state.stop_reason = StopReason.cancelled_by_user.value
        state.transition_log.append(
            {
                "from": previous,
                "to": GoalPhase.CANCELLED.value,
                "at": time.time(),
                "reason": "cancel_recovered_after_restart",
                "attempt": state.attempts,
            }
        )
    # Process-restart recovery: a running/pausing goal cannot be assumed to
    # still be executing after a restart — require an explicit /goal resume.
    elif state.status in (GoalStatus.RUNNING.value, GoalStatus.PAUSING.value):
        state.status = GoalStatus.PAUSED.value
        state.phase = GoalPhase.PAUSED.value
        # The last durable checkpoint is the best available pause boundary.
        # This excludes process downtime from the active-duration budget.
        state.paused_at = state.paused_at or state.updated_at or time.time()
        if state.stop_reason is None:
            state.stop_reason = StopReason.process_restarted.value
    return state


def archive_goal(state: GoalState, workspace: str | Path | None = None) -> Path:
    """Write a full copy of the (terminal) goal into goal-history/.

    The current slot is intentionally kept so ``/goal status`` can still show
    the last terminal state.
    """
    ws = workspace if workspace is not None else state.workspace
    _atomic_write(history_dir(ws) / f"{state.id}.json", state.to_dict())
    return history_dir(ws) / f"{state.id}.json"


def clear_goal_for_test(workspace: str | Path | None = None) -> None:
    """Remove the goal slot + history for a workspace (test/eval cleanup)."""
    path = goal_path(workspace)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    hdir = history_dir(workspace)
    if hdir.exists():
        for item in hdir.glob("goal_*.json"):
            try:
                item.unlink()
            except OSError:
                pass
