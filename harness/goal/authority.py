"""Goal-scoped capabilities used by the generic permission hook.

The permission policy still owns hard denies.  This module only proves that a
direct file mutation requested by a Goal worker is already inside the exact
write boundary supplied by the runner.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class GoalAuthority:
    goal_id: str
    task_id: str
    phase: str
    workspace: Path
    write_roots: tuple[Path, ...]
    forbidden_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class GoalAuthorityDecision:
    allowed: bool
    reason: str
    path: str = ""


_local = threading.local()


def current_goal_authority() -> GoalAuthority | None:
    value = getattr(_local, "authority", None)
    return value if isinstance(value, GoalAuthority) else None


@contextmanager
def goal_authority(
    *,
    goal_id: str,
    task_id: str,
    phase: str,
    workspace: str | Path,
    write_roots: tuple[str, ...] | list[str],
    forbidden_roots: tuple[str, ...] | list[str] = (),
) -> Iterator[GoalAuthority]:
    root = Path(workspace).expanduser().resolve()
    allowed_roots: list[Path] = []
    for item in write_roots:
        if not str(item).strip():
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate.is_relative_to(root):
            allowed_roots.append(candidate)
    forbidden: list[Path] = []
    for item in forbidden_roots:
        if not str(item).strip():
            continue
        try:
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve()
            if candidate.is_relative_to(root):
                forbidden.append(candidate)
        except OSError:
            continue
    authority = GoalAuthority(
        goal_id=str(goal_id),
        task_id=str(task_id),
        phase=str(phase),
        workspace=root,
        write_roots=tuple(dict.fromkeys(allowed_roots)),
        forbidden_roots=tuple(dict.fromkeys(forbidden)),
    )
    previous = getattr(_local, "authority", None)
    _local.authority = authority
    try:
        yield authority
    finally:
        if previous is None:
            try:
                delattr(_local, "authority")
            except AttributeError:
                pass
        else:
            _local.authority = previous


def evaluate_goal_authority(tool_name: str, tool_input: dict[str, Any] | None) -> GoalAuthorityDecision:
    """Authorize only direct writes already bounded by the active Goal Task."""
    authority = current_goal_authority()
    if authority is None:
        return GoalAuthorityDecision(False, "no active Goal authority")
    if tool_name not in {"write_file", "edit_file", "patch_file"}:
        return GoalAuthorityDecision(False, f"{tool_name} is not eligible for Goal scope auto-approval")
    raw_path = str((tool_input or {}).get("path") or "").strip()
    if not raw_path:
        return GoalAuthorityDecision(False, "file tool did not provide a path")
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = authority.workspace / candidate
        candidate = candidate.resolve()
    except OSError as exc:
        return GoalAuthorityDecision(False, f"invalid requested path: {exc}", raw_path)
    try:
        relative = candidate.relative_to(authority.workspace).as_posix()
    except ValueError:
        return GoalAuthorityDecision(False, "requested path is outside the Goal workspace", raw_path)
    if any(candidate == forbidden or candidate.is_relative_to(forbidden) for forbidden in authority.forbidden_roots):
        return GoalAuthorityDecision(False, "requested path is explicitly read-only for this Goal phase", relative)
    if any(candidate == allowed or candidate.is_relative_to(allowed) for allowed in authority.write_roots):
        return GoalAuthorityDecision(
            True,
            f"path is inside Task {authority.task_id} write scope",
            relative,
        )
    return GoalAuthorityDecision(False, "requested path is outside the current Task write scope", relative)
