"""Durable Task graph and Task-owned verification contracts.

Tasks are the only execution unit used by Goal mode. A Task may be worked on
through any number of todos, but only a zero-exit machine verification result
can make it eligible for completion.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.settings import TASKS_DIR

_ORIGINAL_TASKS_DIR = TASKS_DIR
MAX_TASK_HISTORY = 20


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    task_revision: int
    at: float
    outcome: str
    scoped_diff: str = ""
    verification_result_vector: dict = field(default_factory=dict)
    failure_signature: str = ""
    new_evidence_refs: list[str] = field(default_factory=list)
    tested_hypothesis: str = ""


@dataclass(frozen=True)
class FailureRecord:
    id: str
    attempt_id: str
    task_revision: int
    at: float
    failure_signature: str
    failure_mode: str
    blocked_before_assertions: bool
    summary: str
    next_machine_check: str


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
    # Workflow guidance selected during Goal planning. It is deliberately
    # separate from VerificationSpec, which remains the evidence contract.
    skill_names: list[str] = field(default_factory=list)
    verification_spec: dict = field(default_factory=dict)
    verification_state: str = "not_started"
    evidence: list[dict] = field(default_factory=list)
    evaluation: dict | None = None
    evaluation_history: list[dict] = field(default_factory=list)
    repair_history: list[dict] = field(default_factory=list)
    attempt_history: list[dict] = field(default_factory=list)
    failure_history: list[dict] = field(default_factory=list)
    revision: int = 1
    evaluation_required: bool = False
    attempts: int = 0
    last_error: str | None = None
    goal_id: str | None = None
    # Captured when a Goal claims the Task. It scopes evaluator/recovery context
    # to work performed for this Task instead of the entire repository history.
    start_snapshot: str | None = None
    start_diff: str | None = None
    # Hashes of files already dirty when the Task was claimed. They let an
    # evaluator exclude unrelated workspace changes from this Task's diff.
    start_dirty_hashes: dict[str, str] = field(default_factory=dict)
    # Text snapshots of already-dirty files let evaluation attribute a later
    # edit to the current Task instead of treating restored Goal work as new.
    start_dirty_contents: dict[str, str] = field(default_factory=dict)
    # Feature links are durable Task metadata, used to keep Goal/Feature
    # history attributable after a session is restarted.
    feature_ids: list[str] = field(default_factory=list)
    # Planner-declared production scope. Machine gates compare worker diffs
    # against this list before any verification result can advance the Task.
    scope_paths: list[str] = field(default_factory=list)
    # Goal v2 compiles these three scope classes into ``scope_paths`` only at
    # the execution boundary.  Keeping the original classes on the Task makes
    # it possible to show and enforce why a path is writable.
    primary_write: list[str] = field(default_factory=list)
    planned_new: list[str] = field(default_factory=list)
    conditional_write: list[str] = field(default_factory=list)
    read_envelope: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    # Planner rationale for turning acceptance cases into focused proof. It
    # must reach test preparation instead of disappearing at Task projection.
    test_strategy: str = ""
    discovery_revision: int = 0

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
    feature_ids: list[str] | None = None,
    acceptance_cases: list[dict] | None = None,
    skill_names: list[str] | None = None,
    verification_spec: dict | None = None,
    evaluation_required: bool = False,
    scope_paths: list[str] | None = None,
    primary_write: list[str] | None = None,
    planned_new: list[str] | None = None,
    conditional_write: list[str] | None = None,
    read_envelope: list[str] | None = None,
    forbidden: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    test_strategy: str = "",
    discovery_revision: int = 0,
    revision: int = 1,
) -> Task:
    spec = dict(verification_spec or {})
    task = Task(
        id=f"task_{int(time.time())}_{uuid.uuid4().hex[:12]}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=list(blockedBy or []),
        goal_id=goal_id,
        feature_ids=list(feature_ids or []),
        acceptance_cases=list(acceptance_cases or []),
        skill_names=list(skill_names or []),
        verification_spec=spec,
        verification_state=("needs_generation" if spec.get("source") == "needs_generation" else "not_started"),
        evaluation_required=evaluation_required,
        scope_paths=list(scope_paths or [*(primary_write or []), *(planned_new or [])]),
        primary_write=list(primary_write or []),
        planned_new=list(planned_new or []),
        conditional_write=list(conditional_write or []),
        read_envelope=list(read_envelope or []),
        forbidden=list(forbidden or []),
        evidence_refs=list(evidence_refs or []),
        test_strategy=str(test_strategy or "")[:1000],
        discovery_revision=max(0, int(discovery_revision or 0)),
        revision=max(1, int(revision or 1)),
    )
    save_task(task)
    return task


def save_task(task: Task, *, archived: bool = False) -> None:
    path = _archive_path(task.id) if archived else _active_path(task.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{task.id}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(task), handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    attempt = _attempt_record(task, passed=passed, evidence=evidence, error=error)
    task.attempt_history.append(asdict(attempt))
    del task.attempt_history[:-MAX_TASK_HISTORY]
    if passed:
        if not evidence or evidence.get("exit_code") != 0:
            raise ValueError("passing verification requires zero-exit evidence")
        task.evidence.append(dict(evidence))
        task.verification_state = "passing"
        task.last_error = None
        # A previous evaluator verdict is advisory and may describe an older
        # failing/contradictory run. Do not let it block a fresh zero-exit
        # verification; retain it in evaluation_history for audit only.
        task.evaluation = None
    else:
        if evidence:
            task.evidence.append(dict(evidence))
        task.verification_state = "failing"
        task.last_error = error or "verification failed"
        task.failure_history.append(asdict(_failure_record(task, attempt, evidence=evidence, error=task.last_error)))
        del task.failure_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def _attempt_record(task: Task, *, passed: bool, evidence: dict | None, error: str | None) -> AttemptRecord:
    evidence = evidence if isinstance(evidence, dict) else {}
    diagnostics = evidence.get("diagnostics") if isinstance(evidence.get("diagnostics"), dict) else {}
    return AttemptRecord(
        id=f"attempt_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        task_revision=max(1, int(task.revision or 1)),
        at=time.time(),
        outcome="passed" if passed else "failed",
        scoped_diff=str(evidence.get("scoped_diff") or evidence.get("code_snapshot") or "")[:2000],
        verification_result_vector={
            "exit_code": evidence.get("exit_code"),
            "collected_count": evidence.get("collected_count", 0),
            "passed": bool(passed),
        },
        failure_signature=str(diagnostics.get("failure_signature") or ""),
        new_evidence_refs=[str(item) for item in (evidence.get("evidence_refs") or []) if str(item).strip()][:24],
        tested_hypothesis=str(evidence.get("tested_hypothesis") or error or "")[:1000],
    )


def _failure_record(task: Task, attempt: AttemptRecord, *, evidence: dict | None, error: str | None) -> FailureRecord:
    evidence = evidence if isinstance(evidence, dict) else {}
    diagnostics = evidence.get("diagnostics") if isinstance(evidence.get("diagnostics"), dict) else {}
    mode = str(diagnostics.get("failure_mode") or "assertion_failure")
    blocked = bool(diagnostics.get("blocked_before_assertions"))
    next_check = str(diagnostics.get("next_machine_check") or "")
    if not next_check:
        next_check = (
            f"Run a focused {mode} diagnostic probe before changing product code."
            if blocked
            else "Rerun the bound Task verification after one scoped repair."
        )
    return FailureRecord(
        id=f"failure_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        attempt_id=attempt.id,
        task_revision=max(1, int(task.revision or 1)),
        at=time.time(),
        failure_signature=attempt.failure_signature,
        failure_mode=mode,
        blocked_before_assertions=blocked,
        summary=str(error or "verification failed")[:2000],
        next_machine_check=next_check[:2000],
    )


def reopen_blocked_task(task_id: str, *, reason: str = "verification evidence refreshed") -> Task:
    """Reopen a blocked Task after its binding/evidence contract is repaired."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    if task.status != "blocked":
        raise ValueError(f"cannot reopen task in state {task.status!r}")
    task.status = "in_progress"
    task.owner = None
    task.last_error = None
    task.verification_state = "not_started"
    task.repair_history.append({"action": "reopen_for_verification", "reason": reason, "at": time.time()})
    del task.repair_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def record_task_evaluation(task_id: str, evaluation: dict) -> Task:
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.evaluation = dict(evaluation)
    task.evaluation_history.append(dict(evaluation))
    del task.evaluation_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def record_task_repair(task_id: str, repair: dict) -> Task:
    """Append a machine-visible repair decision without losing prior evidence."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.repair_history.append(dict(repair))
    del task.repair_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def record_task_attempt(task_id: str, attempt: AttemptRecord | dict) -> Task:
    """Append one structured attempt record to the current Task revision."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    payload = asdict(attempt) if isinstance(attempt, AttemptRecord) else dict(attempt)
    task.attempt_history.append(payload)
    del task.attempt_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def record_task_failure(task_id: str, failure: FailureRecord | dict) -> Task:
    """Append one structured failure record with a next machine check."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    payload = asdict(failure) if isinstance(failure, FailureRecord) else dict(failure)
    if not str(payload.get("next_machine_check") or "").strip():
        raise ValueError("FailureRecord requires next_machine_check")
    task.failure_history.append(payload)
    del task.failure_history[:-MAX_TASK_HISTORY]
    save_task(task)
    return task


def mark_task_stale(task_id: str, *, reason: str = "verified inputs changed") -> Task:
    """Invalidate a passing Task when its inputs or verification snapshot move."""
    path = _find_task_path(task_id)
    if path is None:
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    if task.verification_state == "passing":
        task.verification_state = "stale"
        task.last_error = reason[:2000]
        task.revision = max(1, int(task.revision or 1)) + 1
        task.repair_history.append({"action": "mark_stale", "reason": reason[:2000], "at": time.time()})
        del task.repair_history[:-MAX_TASK_HISTORY]
        save_task(task, archived=path.parent == _archive_dir())
    return task


def invalidate_stale_passing_tasks(
    task_ids: list[str] | tuple[str, ...],
    *,
    current_snapshot: str,
) -> list[str]:
    """Mark passing Tasks stale when their recorded verification snapshot moves."""
    stale: list[str] = []
    for task_id in task_ids:
        try:
            task = load_task(task_id)
        except (FileNotFoundError, OSError, ValueError):
            continue
        latest = task.evidence[-1] if task.evidence else {}
        recorded = str(latest.get("code_snapshot") or "") if isinstance(latest, dict) else ""
        if task.verification_state == "passing" and recorded and recorded != str(current_snapshot or ""):
            mark_task_stale(task_id, reason="verified workspace snapshot changed")
            stale.append(task_id)
    return stale


def record_task_start(
    task_id: str,
    *,
    snapshot: str | None,
    diff: str | None,
    dirty_hashes: dict[str, str] | None = None,
    dirty_contents: dict[str, str] | None = None,
) -> Task:
    """Persist the Task-local workspace baseline at claim time."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.start_snapshot = snapshot
    task.start_diff = diff
    task.start_dirty_hashes = dict(dirty_hashes or {})
    task.start_dirty_contents = dict(dirty_contents or {})
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
    attempt = _attempt_record(task, passed=passed, evidence=evidence, error=error)
    task.attempt_history.append(asdict(attempt))
    del task.attempt_history[:-MAX_TASK_HISTORY]
    if evidence:
        task.evidence.append(dict(evidence))
    task.verification_state = "passing" if passed else "failing"
    task.last_error = None if passed else (error or "final task verification failed")
    if not passed:
        task.failure_history.append(
            asdict(_failure_record(task, attempt, evidence=evidence, error=task.last_error))
        )
        del task.failure_history[:-MAX_TASK_HISTORY]
    save_task(task, archived=path.parent == _archive_dir())
    return task


def reopen_task_for_goal_repair(task_id: str, *, error: str) -> Task:
    """Return one completed Goal Task to the active board for a real regression.

    Final re-verification can invalidate a previously completed Task.  Reopen
    that Task instead of creating an unbound synthetic task that cannot tell
    the worker which behavior or test actually failed.
    """
    path = _find_task_path(task_id)
    if path is None:
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.status = "pending"
    task.owner = None
    task.completed_at = None
    task.verification_state = "failing"
    task.last_error = error
    task.evaluation = {
        "passed": False,
        "route": "implementation_fix",
        "summary": error,
        "findings": [{"issue": error, "severity": "high", "evidence": "final task verification"}],
    }
    task.evaluation_history.append(dict(task.evaluation))
    del task.evaluation_history[:-MAX_TASK_HISTORY]
    save_task(task)
    if path.parent == _archive_dir():
        try:
            path.unlink()
        except OSError:
            pass
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


def request_task_test_repair(task_id: str, *, coverage_only: bool = False) -> Task:
    """Request additive coverage without discarding the existing binding.

    ``coverage_only`` is used when an already implemented Task needs stronger
    evidence after evaluator review.  Such a test repair must not reopen the
    implementation worker once the new test is bound.
    """
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    spec = dict(task.verification_spec or {})
    spec["previous_selectors"] = list(spec.get("selectors") or [])
    spec["previous_test_files"] = list(spec.get("test_files") or [])
    spec["previous_test_hashes"] = dict(spec.get("test_hashes") or {})
    spec["source"] = "needs_generation"
    spec["allow_posthoc_test"] = True
    if coverage_only:
        spec["coverage_only"] = True
        spec["coverage_repair_count"] = int(spec.get("coverage_repair_count") or 0) + 1
    task.verification_spec = spec
    task.verification_state = "needs_generation"
    task.last_error = "evaluator requested additional focused coverage"
    save_task(task)
    return task


def block_task(task_id: str, *, error: str) -> Task:
    """Leave an explicit Task terminal checkpoint without fabricating success."""
    path = _active_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)
    task = _load_task_from_path(path)
    task.status = "blocked"
    task.owner = None
    task.last_error = str(error or "Task is blocked")[:4_000]
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
    if task.evaluation_required and (task.evaluation or {}).get("passed") is not True:
        return f"Cannot complete {task.id}: required independent evaluation has not passed"
    if task.goal_id and task.acceptance_cases:
        required_cases = {
            str(case.get("id"))
            for case in task.acceptance_cases
            if isinstance(case, dict) and case.get("id")
        }
        spec = task.verification_spec or {}
        selectors = {str(selector) for selector in spec.get("selectors", []) if selector}
        raw_mapping = spec.get("case_selectors")
        mappings = raw_mapping if isinstance(raw_mapping, dict) else {}
        invalid_cases = {
            case_id
            for case_id in required_cases
            if not isinstance(mappings.get(case_id), list)
            or not mappings.get(case_id)
            or any(str(selector) not in selectors for selector in mappings[case_id])
        }
        if invalid_cases:
            missing = ", ".join(sorted(invalid_cases))
            return f"Cannot complete {task.id}: bound tests do not map acceptance cases to collected selectors: {missing}"
        covered_cases = {str(case_id) for case_id in spec.get("covers", [])}
        if not required_cases.issubset(covered_cases):
            missing = ", ".join(sorted(required_cases - covered_cases))
            return f"Cannot complete {task.id}: bound tests do not cover acceptance cases: {missing}"

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
