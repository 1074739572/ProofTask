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
LEASE_FILENAME = "goal.lock"


class GoalStoreError(Exception):
    """Raised for corrupt / unsupported goal state files.

    ``code`` is a stable machine-readable tag: ``goal_state_corrupt``,
    ``unsupported_schema``.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GoalLeaseError(Exception):
    """Raised when another process owns the workspace Goal runner."""


def project_dir(workspace: str | Path | None = None) -> Path:
    if workspace is not None:
        return Path(workspace).expanduser().resolve() / ".project"
    return get_workspace_paths().project_dir


def goal_path(workspace: str | Path | None = None) -> Path:
    return project_dir(workspace) / GOAL_FILENAME


def history_dir(workspace: str | Path | None = None) -> Path:
    return project_dir(workspace) / HISTORY_DIRNAME


def lease_path(workspace: str | Path | None = None) -> Path:
    return project_dir(workspace) / LEASE_FILENAME


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_goal_lease(state: GoalState) -> str:
    """Atomically claim the active Goal slot for one process.

    The JSON Goal state describes durable progress, not process ownership.  A
    separate lease prevents two TUI processes from both normalizing a running
    state and resuming it concurrently.
    """
    path = lease_path(state.workspace or None)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(16).hex()
    payload = {"pid": os.getpid(), "goal_id": state.id, "token": token, "created_at": time.time()}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                pid = int(current.get("pid") or 0) if isinstance(current, dict) else 0
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pid = 0
            if _pid_is_alive(pid):
                raise GoalLeaseError("A Goal runner is already active for this workspace.")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise GoalLeaseError(f"Cannot recover stale Goal lease: {exc}") from exc
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            return token
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    raise GoalLeaseError("Could not acquire the Goal runner lease.")


def release_goal_lease(state: GoalState, token: str | None) -> None:
    if not token:
        return
    path = lease_path(state.workspace or None)
    try:
        current = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(current, dict) and current.get("token") == token:
        try:
            path.unlink()
        except OSError:
            pass


def _lease_has_live_owner(workspace: str | Path | None = None) -> bool:
    """Return whether the workspace lease belongs to a live process.

    Loading state must be read-only.  In particular, a second UI process must
    not reinterpret a still-running Goal as a restart and then mutate it.
    """
    try:
        current = json.loads(lease_path(workspace).read_text(encoding="utf-8", errors="replace"))
        pid = int(current.get("pid") or 0) if isinstance(current, dict) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return _pid_is_alive(pid)


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

    # Older impact-review code classified a completed model response with no
    # parseable JSON as a provider outage. The exact diagnostic proves this is
    # a format failure, so expose the correct recoverable state on load.
    if (
        state.status == GoalStatus.PAUSED.value
        and state.resume_phase == GoalPhase.IMPACT_REVIEW.value
        and state.stop_reason == StopReason.provider_unavailable.value
        and state.last_error == "impact reviewer returned no JSON"
    ):
        state.stop_reason = StopReason.impact_review_format_error.value

    if (
        state.status == GoalStatus.PAUSED.value
        and state.resume_phase == GoalPhase.REPAIR_PLAN.value
        and state.stop_reason == StopReason.provider_unavailable.value
        and state.last_error in {
            "repair planner returned no JSON",
            "repair planner output is not an object",
            "repair action needs instructions",
        }
    ):
        state.stop_reason = StopReason.repair_plan_format_error.value

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
    elif (
        state.status in (GoalStatus.RUNNING.value, GoalStatus.PAUSING.value)
        and not _lease_has_live_owner(workspace)
    ):
        state.resume_phase = state.phase
        state.last_phase = state.phase
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
    lock = lease_path(workspace)
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    hdir = history_dir(workspace)
    if hdir.exists():
        for item in hdir.glob("goal_*.json"):
            try:
                item.unlink()
            except OSError:
                pass
