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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.goal.planner import TaskPlan, plan_tasks
from harness.settings import get_workdir
from harness.verification.catalog import TestCatalog, collect_pytest_catalog

DRAFT_SCHEMA_VERSION = 1
DRAFT_FILENAME = "goal-draft.json"
_DRAFT_STATUSES = frozenset({"clarifying", "ready", "approved"})
_VERIFY_RE = re.compile(r'/goal\s+--verify\s+["\']([^"\']+)["\']')


class GoalDraftError(ValueError):
    pass


@dataclass
class GoalDraft:
    id: str
    target: str
    verification: str
    verification_source: str
    status: str
    questions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    task_plan: list[dict[str, Any]] = field(default_factory=list)
    test_catalog_count: int = 0
    limits: dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
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
    draft.updated_at = time.time()
    _atomic_write(draft_path(workspace), draft.to_dict())


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
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return []
    questions: list[str] = []
    for item in data["questions"][:3]:
        question = str(item).strip()[:500]
        if question and question not in questions:
            questions.append(question)
    return questions


def _intake_prompt(target: str, verification: str, catalog: TestCatalog) -> str:
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


def _plan(draft: GoalDraft, workspace: Path, catalog: TestCatalog, planner_runner=None) -> None:
    plans = plan_tasks(
        _planner_target(draft),
        draft.verification,
        workspace,
        planner_runner=planner_runner,
        test_catalog=catalog,
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
) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    existing = load_draft(root)
    if existing and existing.status in {"clarifying", "ready", "approved"}:
        raise GoalDraftError("A Goal draft already exists. Use /goal preview, answer, revise, approve, or discard.")
    command, source = infer_verification(root, verification)
    catalog = collect_pytest_catalog(root)
    draft = GoalDraft(
        id=f"goal_draft_{int(time.time())}_{uuid.uuid4().hex[:4]}",
        target=target.strip(),
        verification=command,
        verification_source=source,
        status="clarifying",
        limits=dict(limits or {}),
        test_catalog_count=len(catalog.selectors),
    )
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
                    max_rounds=12,
                    stats=stats,
                )
            except Exception:
                raw = ""
        else:
            raw = runner(target=draft.target, verification=command, catalog=catalog)
        draft.questions = _questions_from_raw(raw)
    if not draft.questions:
        _plan(draft, root, catalog, planner_runner)
    save_draft(draft, root)
    return draft


def answer_draft(answer: str, *, workspace: Path | None = None, planner_runner=None) -> GoalDraft:
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
    catalog = collect_pytest_catalog(root)
    draft.test_catalog_count = len(catalog.selectors)
    if draft.unanswered_question is None:
        _plan(draft, root, catalog, planner_runner)
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


def format_draft(draft: GoalDraft) -> str:
    lines = [
        f"Goal draft: {draft.id} [{draft.status}]",
        f"  Target: {draft.target}",
        f"  Global verification: {draft.verification or 'needs your answer'} ({draft.verification_source})",
        f"  Collected pytest selectors: {draft.test_catalog_count}",
    ]
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
