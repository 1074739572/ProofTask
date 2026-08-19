"""User-confirmed intake for Goal execution.

The draft is deliberately separate from :mod:`harness.goal.models`: it is a
read-only planning conversation, not an executing Goal.  No implementation or
test file may be written until the user approves a ready draft.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.goal.planner import TaskPlan, plan_tasks
from harness.settings import get_workdir
from harness.verification import VerificationContext, select_adapter

DRAFT_SCHEMA_VERSION = 2
DRAFT_FILENAME = "goal-draft.json"
_DRAFT_STATUSES = frozenset({"clarifying", "discovering", "planning", "paused", "ready", "approved", "failed", "consumed"})
_VERIFY_RE = re.compile(r'/goal\s+--verify\s+["\']([^"\']+)["\']')
_AGENT_RESULT_HEADER_RE = re.compile(r"^\[[^\]]+\][^\n]*\n+")


class GoalDraftError(ValueError):
    pass


@dataclass
class GoalDraft:
    id: str
    target: str
    verification: str
    verification_source: str
    status: str
    verification_adapter: str = "pytest"
    questions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    task_plan: list[dict[str, Any]] = field(default_factory=list)
    test_catalog_count: int = 0
    limits: dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stage: str = "created"
    stage_started_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    stage_deadline: float = 0.0
    input_hash: str = ""
    discovery_path: str = ""
    last_error: str | None = None
    schema_version: int = DRAFT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalDraft":
        if data.get("schema_version") != DRAFT_SCHEMA_VERSION:
            raise GoalDraftError("unsupported Goal draft schema")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise GoalDraftError(f"unsupported Goal draft fields: {sorted(unknown)}")
        if data.get("status") not in _DRAFT_STATUSES:
            raise GoalDraftError(f"invalid Goal draft status: {data.get('status')!r}")
        return cls(**data)

    @property
    def unanswered_question(self) -> str | None:
        return self.questions[len(self.answers)] if len(self.answers) < len(self.questions) else None


def _project_dir(workspace: Path | None = None) -> Path:
    return (workspace or get_workdir()).resolve() / ".project"


def draft_path(workspace: Path | None = None) -> Path:
    return _project_dir(workspace) / DRAFT_FILENAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".goal-draft.", suffix=".tmp", dir=path.parent)
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


def load_draft(workspace: Path | None = None) -> GoalDraft | None:
    path = draft_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalDraftError(f"cannot read Goal draft: {exc}") from exc
    if not isinstance(data, dict):
        raise GoalDraftError("Goal draft is not a JSON object")
    return GoalDraft.from_dict(data)


def save_draft(draft: GoalDraft, workspace: Path | None = None) -> None:
    now = time.time()
    draft.updated_at = now
    draft.last_heartbeat = now
    _atomic_write(draft_path(workspace), draft.to_dict())


def _set_stage(draft: GoalDraft, stage: str, *, deadline: float | None = None) -> None:
    now = time.time()
    draft.stage = stage
    draft.stage_started_at = now
    draft.last_heartbeat = now
    if deadline is not None:
        draft.stage_deadline = deadline


def _input_hash(target: str, verification: str, limits: dict[str, int] | None) -> str:
    payload = json.dumps({"target": target, "verification": verification, "limits": limits or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discard_draft(workspace: Path | None = None) -> None:
    path = draft_path(workspace)
    if path.exists():
        path.unlink()


def infer_verification(workspace: Path, explicit: str | None = None) -> tuple[str, str]:
    """Return a conservative global regression command and where it came from."""
    if explicit:
        return explicit, "user"
    handbook = workspace / "HARNESS.md"
    if handbook.exists():
        match = _VERIFY_RE.search(handbook.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1), "HARNESS.md"
    if (workspace / "pytest.ini").exists() or (workspace / "tests").exists():
        return "python -m pytest -q", "pytest discovery"
    package = workspace / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("test"):
            return "npm test", "package.json"
    return "", "not inferred"


def _questions_from_raw(raw: str) -> list[str]:
    # ``run_agent_task`` prepends a human-readable execution header. Intake
    # still returns JSON underneath it, so parse the model payload rather than
    # treating a successful clarification as an empty answer.
    raw = _AGENT_RESULT_HEADER_RE.sub("", (raw or "").lstrip(), count=1).strip()
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GoalDraftError(f"Goal intake returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise GoalDraftError("Goal intake returned invalid JSON: expected an object with a questions list")
    questions: list[str] = []
    for item in data["questions"][:3]:
        question = str(item).strip()[:500]
        if question and question not in questions:
            questions.append(question)
    return questions


def _collect_catalog(workspace: Path, verification: str):
    adapter = select_adapter(workspace, verification)
    return adapter, adapter.discover(VerificationContext(workspace, command=verification))


def _intake_prompt(target: str, verification: str, catalog) -> str:
    return (
        "You are the read-only intake agent for a verified coding Goal.\n"
        "Read the repository only when needed. Identify only decisions the user must make; "
        "do not ask about facts discoverable from the repository.\n"
        f"Requested outcome: {target}\n"
        f"Proposed global verification: {verification or 'not inferred'}\n"
        f"{catalog.prompt_text()}\n\n"
        "Reply ONLY as JSON: {\"questions\":[\"short question\"]}. "
        "Use an empty list when the requirement is sufficiently clear. Never propose implementation."
    )


def _planner_target(draft: GoalDraft) -> str:
    if not draft.answers:
        return draft.target
    pairs = "\n".join(f"Q: {question}\nA: {answer}" for question, answer in zip(draft.questions, draft.answers))
    return f"{draft.target}\n\nConfirmed clarifications:\n{pairs}"


def _plan(draft: GoalDraft, workspace: Path, catalog, planner_runner=None, discovery_manifest=None, verification_adapter=None) -> None:
    plans = plan_tasks(
        _planner_target(draft),
        draft.verification,
        workspace,
        planner_runner=planner_runner,
        test_catalog=catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
    )
    draft.task_plan = [plan.to_dict() for plan in plans]
    draft.status = "ready"


def create_draft(
    target: str,
    *,
    verification: str | None = None,
    limits: dict[str, int] | None = None,
    workspace: Path | None = None,
    intake_runner: Callable[..., str] | None = None,
    planner_runner=None,
    discovery_runner=None,
) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    existing = load_draft(root)
    if existing and existing.status != "consumed":
        raise GoalDraftError("A Goal draft already exists. Use /goal preview, answer, revise, approve, or discard.")
    command, source = infer_verification(root, verification)
    operation_timeout = max(1, int((limits or {}).get("operation_timeout_seconds", 1800)))
    draft_id = f"goal_draft_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    draft = GoalDraft(
        id=draft_id,
        target=target.strip(),
        verification=command,
        verification_source=source,
        status="clarifying",
        limits=dict(limits or {}),
        test_catalog_count=0,
        stage="discovering",
        input_hash=_input_hash(target.strip(), command, limits),
        stage_deadline=time.time() + operation_timeout,
        discovery_path=f".project/goal-memory/{draft_id}/discovery",
    )
    # Persist a recovery point before test collection or any model request.
    # The catalog is filled below after this write.
    draft.test_catalog_count = 0
    save_draft(draft, root)
    try:
        adapter, catalog = _collect_catalog(root, command)
        draft.verification_adapter = getattr(adapter, "id", "pytest")
        draft.test_catalog_count = len(catalog.selectors)
        _set_stage(draft, "intake")
        save_draft(draft, root)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"test catalog failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root)
        raise GoalDraftError(draft.last_error) from exc
    if not command:
        draft.questions = ["没有找到可靠的全局回归命令。请告诉我最终应运行的测试命令。"]
    else:
        runner = intake_runner
        if runner is None:
            from harness.agents.runner import AgentTaskStats, run_agent_task

            stats = AgentTaskStats()
            try:
                raw = run_agent_task(
                    description="clarify verified coding goal",
                    prompt=_intake_prompt(draft.target, command, catalog),
                    agent_type="goal_intake",
                    cwd=str(root),
                    # Intake is a user-decision gate, not repository research.
                    # The planner receives the test catalog and owns the bounded
                    # code inspection that follows a clear intake.
                    max_rounds=1,
                    tools_override=(),
                    stats=stats,
                    deadline=time.monotonic() + operation_timeout,
                )
            except Exception as exc:
                draft.status = "paused"
                draft.last_error = f"Goal intake request failed: {type(exc).__name__}: {exc}"
                _set_stage(draft, "paused")
                save_draft(draft, root)
                raise GoalDraftError(draft.last_error) from exc
            if raw.startswith("[goal_intake] failed:") or raw.startswith("[goal_intake] stopped:"):
                draft.status = "paused"
                draft.last_error = f"Goal intake is unavailable: {raw}"
                _set_stage(draft, "paused")
                save_draft(draft, root)
                raise GoalDraftError(draft.last_error)
        else:
            try:
                raw = runner(target=draft.target, verification=command, catalog=catalog, deadline=time.monotonic() + operation_timeout)
            except TypeError as exc:
                # Keep the small injectable test/provider contract usable while
                # the built-in runner receives the deadline.
                if "deadline" not in str(exc):
                    raise
                raw = runner(target=draft.target, verification=command, catalog=catalog)
        try:
            draft.questions = _questions_from_raw(raw)
        except GoalDraftError as exc:
            draft.status = "paused"
            draft.last_error = str(exc)
            _set_stage(draft, "paused")
            save_draft(draft, root)
            raise
    if draft.questions:
        _set_stage(draft, "clarifying")
        save_draft(draft, root)
    if not draft.questions:
        discovery_manifest = None
        if planner_runner is None:
            _set_stage(draft, "discovering")
            save_draft(draft, root)
            try:
                if discovery_runner is None:
                    from harness.goal.discovery import DiscoverySupervisor

                    discovery_manifest = DiscoverySupervisor().run(
                        goal_id=draft.id,
                        target=draft.target,
                        workspace=root,
                        deadline=time.monotonic() + operation_timeout,
                    ).to_dict()
                else:
                    discovery_manifest = discovery_runner(draft=draft, workspace=root)
                draft.discovery_path = f".project/goal-memory/{draft.id}/discovery/manifest.json"
            except Exception as exc:
                draft.status = "paused"
                draft.last_error = f"Goal discovery failed: {type(exc).__name__}: {exc}"
                _set_stage(draft, "paused")
                save_draft(draft, root)
                raise GoalDraftError(draft.last_error) from exc
        _set_stage(draft, "planning")
        save_draft(draft, root)
        try:
            _plan(draft, root, catalog, planner_runner, discovery_manifest, adapter)
        except Exception as exc:
            draft.status = "paused"
            draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
            _set_stage(draft, "paused")
            save_draft(draft, root)
            raise GoalDraftError(draft.last_error) from exc
    save_draft(draft, root)
    return draft


def _run_discovery_and_plan(draft: GoalDraft, root: Path, catalog, adapter, *, planner_runner=None, discovery_runner=None) -> None:
    operation_timeout = max(1, int(draft.limits.get("operation_timeout_seconds", 1800)))
    _set_stage(draft, "discovering", deadline=time.time() + operation_timeout)
    save_draft(draft, root)
    try:
        if discovery_runner is None:
            from harness.goal.discovery import DiscoverySupervisor

            manifest = DiscoverySupervisor().run(
                goal_id=draft.id, target=_planner_target(draft), workspace=root,
                deadline=time.monotonic() + operation_timeout,
            ).to_dict()
        else:
            manifest = discovery_runner(draft=draft, workspace=root)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"Goal discovery failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root)
        raise GoalDraftError(draft.last_error) from exc
    failed_jobs = [job for job in manifest.get("jobs", []) if isinstance(job, dict) and job.get("retryable")]
    if failed_jobs:
        draft.status = "paused"
        draft.last_error = "Discovery is rate limited; wait, then run /goal resume."
        _set_stage(draft, "paused")
        save_draft(draft, root)
        raise GoalDraftError(draft.last_error)
    draft.discovery_path = f".project/goal-memory/{draft.id}/discovery/manifest.json"
    _set_stage(draft, "planning", deadline=time.time() + operation_timeout)
    save_draft(draft, root)
    try:
        _plan(draft, root, catalog, planner_runner, manifest, adapter)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root)
        raise GoalDraftError(draft.last_error) from exc


def answer_draft(answer: str, *, workspace: Path | None = None, planner_runner=None, discovery_runner=None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None or draft.status != "clarifying":
        raise GoalDraftError("No Goal draft is waiting for clarification.")
    text = answer.strip()
    if not text:
        raise GoalDraftError("Clarification answer must not be empty.")
    draft.answers.append(text[:1000])
    if not draft.verification:
        draft.verification = text
        draft.verification_source = "user clarification"
    adapter, catalog = _collect_catalog(root, draft.verification)
    draft.verification_adapter = getattr(adapter, "id", "pytest")
    draft.test_catalog_count = len(catalog.selectors)
    if draft.unanswered_question is None:
        # Test/provider injectors may supply only a planner. Production always
        # takes the Discovery path; an injected discovery runner exercises the
        # same route without a live model request.
        if planner_runner is None or discovery_runner is not None:
            _run_discovery_and_plan(draft, root, catalog, adapter, planner_runner=planner_runner, discovery_runner=discovery_runner)
        else:
            _set_stage(draft, "planning")
            save_draft(draft, root)
            _plan(draft, root, catalog, planner_runner, verification_adapter=adapter)
    save_draft(draft, root)
    return draft


def resume_draft(*, workspace: Path | None = None, planner_runner=None, discovery_runner=None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None or draft.status != "paused":
        raise GoalDraftError("No paused Goal draft to resume.")
    if draft.unanswered_question:
        draft.status = "clarifying"
        _set_stage(draft, "clarifying")
        save_draft(draft, root)
        return draft
    adapter, catalog = _collect_catalog(root, draft.verification)
    draft.verification_adapter = getattr(adapter, "id", "pytest")
    draft.test_catalog_count = len(catalog.selectors)
    _run_discovery_and_plan(draft, root, catalog, adapter, planner_runner=planner_runner, discovery_runner=discovery_runner)
    save_draft(draft, root)
    return draft


def approve_draft(*, workspace: Path | None = None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None:
        raise GoalDraftError("No Goal draft to approve.")
    if draft.status not in {"ready", "approved"} or not draft.task_plan or not draft.verification:
        raise GoalDraftError("Goal draft is not ready. Answer the outstanding questions first.")
    draft.status = "approved"
    save_draft(draft, root)
    return draft


def validate_ready_draft(*, workspace: Path | None = None) -> GoalDraft:
    """Load a ready draft without changing its lifecycle state.

    The command layer uses this before starting the Goal. This keeps a failed
    startup retryable instead of briefly consuming/approving the draft first.
    """
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None:
        raise GoalDraftError("No Goal draft to approve.")
    if draft.status not in {"ready", "approved"} or not draft.task_plan or not draft.verification:
        raise GoalDraftError("Goal draft is not ready. Answer the outstanding questions first.")
    return draft


def mark_draft_consumed(*, workspace: Path | None = None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None:
        raise GoalDraftError("No Goal draft to consume.")
    draft.status = "consumed"
    draft.last_error = None
    _set_stage(draft, "consumed")
    save_draft(draft, root)
    return draft


def mark_draft_start_failed(error: str, *, workspace: Path | None = None) -> GoalDraft | None:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None:
        return None
    draft.status = "ready"
    draft.last_error = str(error)[:1000]
    _set_stage(draft, "ready")
    save_draft(draft, root)
    return draft


def format_draft(draft: GoalDraft) -> str:
    lines = [
        f"Goal draft: {draft.id} [{draft.status}]",
        f"  Target: {draft.target}",
        f"  Global verification: {draft.verification or 'needs your answer'} ({draft.verification_source})",
        f"  Verification adapter: {draft.verification_adapter}; collected selectors: {draft.test_catalog_count}",
        f"  Stage: {draft.stage} (heartbeat {int(max(0, time.time() - draft.last_heartbeat))}s ago)",
    ]
    if draft.last_error:
        lines.append(f"  Last error: {draft.last_error[:200]}")
    if draft.unanswered_question:
        lines.append(f"  Question: {draft.unanswered_question}")
        lines.append("  Reply naturally in the TUI, or use: /goal answer <answer>")
    for index, plan in enumerate(draft.task_plan, start=1):
        spec = plan.get("verification_spec") or {}
        source = spec.get("source", "needs_generation")
        selectors = ", ".join(spec.get("selectors") or []) or "focused test will be generated after approval"
        lines.append(f"  Task {index}: {plan.get('name')} [{source}]\n    Tests: {selectors}")
    if draft.status == "ready":
        lines.append("  Review complete. Run /goal approve to write tests and begin execution.")
    return "\n".join(lines)
