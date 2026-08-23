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
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.goal.language import detect_goal_language, human_language_label
from harness.goal.planner import TaskPlan, discovery_readiness_error, plan_tasks
from harness.settings import get_workdir
from harness.verification import VerificationContext, select_adapter

DRAFT_SCHEMA_VERSION = 5
DRAFT_FILENAME = "goal-draft.json"
_DRAFT_STATUSES = frozenset({"clarifying", "discovering", "planning", "paused", "cancelled", "ready", "approved", "failed", "consumed"})
_ACTIVE_DRAFT_STAGES = frozenset({"preflight", "catalog", "intake", "discovering", "planning"})
_VERIFY_RE = re.compile(r'/goal\s+--verify\s+["\']([^"\']+)["\']')
_DRAFT_IO_LOCK = threading.RLock()
_AGENT_RESULT_HEADER_RE = re.compile(r"^\[[^\]]+\][^\n]*\n+")
_PROJECT_MARKERS = frozenset({"package.json", "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py"})
_PROJECT_EXCLUDED_DIRS = frozenset({".git", ".project", ".venv", "venv", "node_modules", "dist", "build", "coverage", ".worktrees"})


class GoalDraftError(ValueError):
    pass


@dataclass(frozen=True)
class GoalIntakeResult:
    questions: list[str]
    summary: str
    assumptions: list[str]


@dataclass
class GoalDraft:
    id: str
    target: str
    verification: str
    verification_source: str
    status: str
    # Relative to the workspace that owns .project. Planning and execution use
    # this project, while durable Goal state remains in the workspace root.
    project_root: str = "."
    verification_adapter: str = "pytest"
    # Language for user-facing model artifacts. Internal protocol values such
    # as JSON keys, paths, commands, and selectors remain unchanged.
    language: str = "en"
    questions: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    intake_summary: str = ""
    intake_assumptions: list[str] = field(default_factory=list)
    # Frozen only after the planning reviewer accepts the plan.  This is the
    # decision contract handed to /goal approve and then to every worker.
    goal_contract: dict[str, Any] = field(default_factory=dict)
    planning_review: dict[str, Any] = field(default_factory=dict)
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
    # The durable checkpoint used by /goal resume.  The visible stage becomes
    # "paused" after an error, so it cannot by itself tell us whether the
    # expensive Discovery work needs to be repeated.
    resume_from: str = "preflight"
    schema_version: int = DRAFT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalDraft":
        version = data.get("schema_version")
        if version != DRAFT_SCHEMA_VERSION:
            raise GoalDraftError("unsupported Goal draft schema")
        data = dict(data)
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


def resolve_target_project(workspace: Path, target: str) -> Path:
    """Return the deepest project marker explicitly named by a Goal target."""
    root = workspace.resolve()
    normalized = target.replace("\\", "/").lower()
    candidates = [root]
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _PROJECT_EXCLUDED_DIRS]
        if not _PROJECT_MARKERS.intersection(files):
            continue
        candidate = Path(current).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative == ".":
            continue
        escaped = re.escape(relative.lower())
        if re.search(rf"(?<![a-z0-9_.-]){escaped}(?:/|$)", normalized):
            candidates.append(candidate)
    return max(candidates, key=lambda path: len(path.relative_to(root).parts))


def _legacy_resume_stage(data: dict[str, Any]) -> str:
    error = str(data.get("last_error") or "").lower()
    if "intake" in error:
        return "intake"
    if "catalog" in error:
        return "catalog"
    if "planning" in error or "planner" in error:
        return "planning"
    if "discovery" in error:
        return "discovering"
    stage = str(data.get("stage") or "")
    return stage if stage in _ACTIVE_DRAFT_STAGES else "discovering"


def draft_project_root(draft: GoalDraft, workspace: Path) -> Path:
    """Resolve a Draft's scoped project without allowing it outside the workspace."""
    root = workspace.resolve()
    try:
        candidate = (root / (draft.project_root or ".")).resolve()
        return candidate if candidate.is_relative_to(root) and candidate.is_dir() else root
    except OSError:
        return root


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


def _draft_event_payload(draft: GoalDraft, *, event: str, message: str | None = None) -> dict[str, Any]:
    """Return a bounded, secret-free snapshot for JSONL frontends.

    Draft events intentionally contain provider/model *labels* only.  API key
    values and raw model output never belong in the event stream.
    """
    task_summaries: list[dict[str, Any]] = []
    for plan in draft.task_plan:
        if not isinstance(plan, dict):
            continue
        spec = plan.get("verification_spec") if isinstance(plan.get("verification_spec"), dict) else {}
        selectors = spec.get("selectors") if isinstance(spec.get("selectors"), list) else []
        cases = plan.get("acceptance_cases") if isinstance(plan.get("acceptance_cases"), list) else []
        dependencies = plan.get("depends_on") if isinstance(plan.get("depends_on"), list) else []
        primary_write = plan.get("primary_write") if isinstance(plan.get("primary_write"), list) else []
        planned_new = plan.get("planned_new") if isinstance(plan.get("planned_new"), list) else []
        conditional_write = plan.get("conditional_write") if isinstance(plan.get("conditional_write"), list) else []
        evidence_refs = plan.get("evidence_refs") if isinstance(plan.get("evidence_refs"), list) else []
        task_summaries.append(
            {
                "name": str(plan.get("name") or "Unnamed Task")[:300],
                "behavior": str(plan.get("behavior") or "")[:600],
                "depends_on": [str(item)[:200] for item in dependencies],
                "acceptance_count": len(cases),
                "verification_source": str(spec.get("source") or "needs_generation")[:80],
                "selectors": [str(item)[:500] for item in selectors[:8]],
                "primary_write": [str(item)[:500] for item in primary_write[:12]],
                "planned_new": [str(item)[:500] for item in planned_new[:12]],
                "conditional_write": [str(item)[:500] for item in conditional_write[:12]],
                "evidence_refs": [str(item)[:120] for item in evidence_refs[:12]],
                "test_strategy": str(plan.get("test_strategy") or "")[:1_000],
            }
        )
    payload: dict[str, Any] = {
        "id": draft.id,
        "target": draft.target[:1_000],
        "verification": draft.verification[:1_000],
        "verification_source": draft.verification_source[:200],
        "verification_adapter": draft.verification_adapter[:80],
        "status": draft.status,
        "stage": draft.stage,
        "resume_from": draft.resume_from,
        "event": event,
        "updated_at": draft.updated_at,
        "stage_started_at": draft.stage_started_at,
        "last_heartbeat": draft.last_heartbeat,
        "stage_deadline": draft.stage_deadline,
        "test_catalog_count": draft.test_catalog_count,
        "discovery_path": draft.discovery_path,
        "intake_summary": draft.intake_summary[:1_000],
        "intake_assumptions": [str(item)[:500] for item in draft.intake_assumptions[:8]],
        "goal_contract": dict(draft.goal_contract or {}),
        "planning_review": dict(draft.planning_review or {}),
        "clarifications": [
            {"question": str(question)[:500], "answer": str(answer)[:1_000]}
            for question, answer in list(zip(draft.questions, draft.answers))[:3]
        ],
        "last_error": (draft.last_error or "")[:2_000],
        "question": draft.unanswered_question or "",
        "question_index": len(draft.answers),
        "question_count": len(draft.questions),
        "task_count": len(draft.task_plan),
        "tasks": task_summaries,
    }
    if message:
        payload["message"] = str(message)[:1_000]
    return payload


def _emit_draft_event(draft: GoalDraft, *, event: str = "updated", message: str | None = None) -> None:
    """Mirror durable Draft progress when an event-stream frontend is active."""
    try:
        from harness.ui.events import emit, is_enabled

        if is_enabled():
            emit("goal_draft_status", **_draft_event_payload(draft, event=event, message=message))
    except Exception:
        # UI telemetry must never make the Goal pipeline fail.
        return


def load_draft(workspace: Path | None = None) -> GoalDraft | None:
    path = draft_path(workspace)
    with _DRAFT_IO_LOCK:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GoalDraftError(f"cannot read Goal draft: {exc}") from exc
        if not isinstance(data, dict):
            raise GoalDraftError("Goal draft is not a JSON object")
        return GoalDraft.from_dict(data)


def emit_current_draft_status(*, workspace: Path | None = None, event: str = "hydrated") -> GoalDraft | None:
    """Emit an existing Draft as startup hydration or an explicit status response."""
    draft = load_draft(workspace)
    if draft is not None and draft.status != "consumed":
        _emit_draft_event(draft, event=event)
    return draft


def save_draft(
    draft: GoalDraft,
    workspace: Path | None = None,
    *,
    event: str = "updated",
    message: str | None = None,
    preserve_control: bool = True,
) -> GoalDraft:
    now = time.time()
    draft.updated_at = now
    draft.last_heartbeat = now
    with _DRAFT_IO_LOCK:
        path = draft_path(workspace)
        # A foreground model call can finish after a user has paused or
        # cancelled it. Preserve that durable control request instead of
        # reviving the local pre-call copy of the Draft.
        if path.exists():
            try:
                stored_data = json.loads(path.read_text(encoding="utf-8"))
                stored = GoalDraft.from_dict(stored_data) if isinstance(stored_data, dict) else None
            except (OSError, json.JSONDecodeError, GoalDraftError):
                stored = None
            if (
                preserve_control
                and
                stored is not None
                and stored.id == draft.id
                and stored.status in {"paused", "cancelled"}
                and draft.status not in {"paused", "cancelled"}
            ):
                _emit_draft_event(stored, event="control_preserved")
                return stored
        _atomic_write(draft_path(workspace), draft.to_dict())
    _emit_draft_event(draft, event=event, message=message)
    return draft


def touch_draft(workspace: Path | None = None, *, event: str = "heartbeat") -> GoalDraft | None:
    """Refresh the durable Draft heartbeat without changing its stage."""
    root = (workspace or get_workdir()).resolve()
    with _DRAFT_IO_LOCK:
        draft = load_draft(root)
        if draft is None:
            return None
        now = time.time()
        draft.updated_at = now
        draft.last_heartbeat = now
        _atomic_write(draft_path(root), draft.to_dict())
    _emit_draft_event(draft, event=event)
    return draft


@contextmanager
def _draft_heartbeat(draft: GoalDraft, workspace: Path, *, interval: float = 2.0):
    """Keep a slow external operation visibly and durably alive."""
    stop = threading.Event()

    def worker() -> None:
        while not stop.wait(max(0.25, interval)):
            try:
                touch_draft(workspace)
            except Exception:
                # The foreground operation owns the authoritative error path.
                pass

    thread = threading.Thread(target=worker, name=f"goal-draft-heartbeat-{draft.id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(0.5, interval + 0.5))


def _set_stage(draft: GoalDraft, stage: str, *, deadline: float | None = None) -> None:
    now = time.time()
    draft.stage = stage
    draft.stage_started_at = now
    draft.last_heartbeat = now
    if stage in _ACTIVE_DRAFT_STAGES:
        draft.resume_from = stage
    if deadline is not None:
        draft.stage_deadline = deadline


def _input_hash(target: str, verification: str, limits: dict[str, int] | None) -> str:
    payload = json.dumps({"target": target, "verification": verification, "limits": limits or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discard_draft(workspace: Path | None = None) -> None:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    path = draft_path(root)
    if draft is not None:
        draft.status = "cancelled"
        _set_stage(draft, "cancelled")
        _emit_draft_event(draft, event="discarded", message="Goal draft discarded")
    if path.exists():
        path.unlink()


def pause_draft(*, workspace: Path | None = None, cancelled: bool = False) -> str:
    """Pause or cancel an in-flight Draft operation immediately."""
    from harness.agent.cancel import request_cancel

    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None or draft.stage not in _ACTIVE_DRAFT_STAGES:
        return "No active Goal draft operation to pause."
    request_cancel()
    draft.status = "cancelled" if cancelled else "paused"
    draft.last_error = "Goal draft cancelled by user." if cancelled else "Goal draft paused by user."
    _set_stage(draft, "cancelled" if cancelled else "paused")
    save_draft(draft, root, event="paused", message=draft.last_error)
    action = "cancelled" if cancelled else "paused"
    suffix = "Start a new /goal draft when ready." if cancelled else "Normal chat is available; use /goal resume to continue."
    return f"Goal draft {action}. {suffix}"


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


def _intake_from_raw(raw: str) -> GoalIntakeResult:
    # ``run_agent_task`` prepends a human-readable execution header. Intake
    # still returns JSON underneath it, so parse the model payload rather than
    # treating a successful clarification as an empty answer.
    raw = _AGENT_RESULT_HEADER_RE.sub("", (raw or "").lstrip(), count=1).strip()
    # Accept a fenced or lightly narrated JSON object. Reasoning providers
    # sometimes wrap the required object in a short sentence even when the
    # prompt asks for JSON-only output.
    if raw and not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
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
    summary = str(data.get("summary") or "").strip()[:1_000]
    assumptions: list[str] = []
    raw_assumptions = data.get("assumptions")
    if raw_assumptions is not None and not isinstance(raw_assumptions, list):
        raise GoalDraftError("Goal intake returned invalid JSON: assumptions must be a list")
    for item in (raw_assumptions or [])[:8]:
        assumption = str(item).strip()[:500]
        if assumption and assumption not in assumptions:
            assumptions.append(assumption)
    # Providers configured before this protocol extension may still return
    # only {"questions": [...]}. Keep that response valid while ensuring the
    # UI always has an explicit intake decision to present.
    if not summary:
        summary = (
            "The requirement has unresolved user decisions listed below."
            if questions
            else "The requirement is clear enough to continue without clarification."
        )
    return GoalIntakeResult(questions=questions, summary=summary, assumptions=assumptions)


def _apply_intake_result(draft: GoalDraft, result: GoalIntakeResult) -> None:
    draft.questions = result.questions
    draft.intake_summary = result.summary
    draft.intake_assumptions = result.assumptions


def _question_key(question: str) -> str:
    """Return a conservative identity key used to reject repeated intake loops."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", question.casefold())).strip()


def _append_followup_intake_result(draft: GoalDraft, result: GoalIntakeResult) -> None:
    """Keep confirmed Q/A intact while adding only genuinely new questions."""
    known = {_question_key(question) for question in draft.questions}
    additions: list[str] = []
    for question in result.questions:
        key = _question_key(question)
        if not key or key in known:
            raise GoalDraftError(
                "Goal intake repeated a question that was already asked. "
                "The draft was paused so it cannot loop or discard your answer."
            )
        known.add(key)
        additions.append(question)
    draft.questions.extend(additions)
    if result.summary:
        draft.intake_summary = result.summary
    for assumption in result.assumptions:
        if assumption not in draft.intake_assumptions:
            draft.intake_assumptions.append(assumption)


def _collect_catalog(workspace: Path, verification: str):
    adapter = select_adapter(workspace, verification)
    return adapter, adapter.discover(VerificationContext(workspace, command=verification))


def _required_draft_agents(*, intake_runner=None, planner_runner=None, discovery_runner=None) -> tuple[str, ...]:
    """Select only routes that this invocation will actually call.

    Test injectors and offline callers should not be forced to configure an
    API key for a model they deliberately replaced. Production calls, however,
    get a complete static route check before spending a token.
    """
    from harness.goal.preflight import DISCOVERY_AGENT_TYPES

    required: list[str] = []
    if intake_runner is None:
        required.append("goal_intake")
    if planner_runner is None:
        required.append("goal_planner")
        if discovery_runner is None:
            required.extend(DISCOVERY_AGENT_TYPES)
    return tuple(dict.fromkeys(required))


def _required_resume_agents(*, planner_runner=None, discovery_runner=None, include_discovery: bool = True) -> tuple[str, ...]:
    from harness.goal.preflight import DISCOVERY_AGENT_TYPES

    required = []
    if planner_runner is None:
        required.append("goal_planner")
        if include_discovery and discovery_runner is None:
            required.extend(DISCOVERY_AGENT_TYPES)
    return tuple(dict.fromkeys(required))


def _preflight_or_raise(draft: GoalDraft, root: Path, agent_types: tuple[str, ...]) -> None:
    if not agent_types:
        return
    from harness.goal.preflight import preflight_goal_agents

    report = preflight_goal_agents(agent_types)
    if report.ok:
        return
    draft.status = "paused"
    draft.last_error = report.format(title="Goal provider preflight failed")[:4_000]
    _set_stage(draft, "paused")
    save_draft(draft, root, event="failed", message="Static provider/model preflight failed")
    raise GoalDraftError(draft.last_error)


def _manifest_failures(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for job in manifest.get("jobs", []) if isinstance(manifest, dict) else []:
        if not isinstance(job, dict):
            continue
        error = str(job.get("error") or "").strip()
        if error or str(job.get("status") or "") in {"failed", "cancelled", "timeout"}:
            failures.append(job)
    return failures


def _manifest_successes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        job for job in manifest.get("jobs", [])
        if isinstance(job, dict) and str(job.get("status") or "") in {"done", "completed"}
        and not str(job.get("error") or "").strip()
    ]


def _is_reusable_discovery_manifest(manifest: object) -> bool:
    """Return whether a saved manifest is structurally safe to plan from."""
    if not isinstance(manifest, dict):
        return False
    return all(isinstance(manifest.get(key), list) for key in ("repo_files", "evidence", "jobs"))


def _format_discovery_failures(failures: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for job in failures:
        role = str(job.get("role") or job.get("id") or "unknown")
        error = str(job.get("error") or job.get("status") or "failed").replace("\n", " ")
        rows.append(f"{role}: {error[:600]}")
    return "Goal discovery paused; no planner call was made. " + " | ".join(rows[:8])


def _intake_prompt(target: str, verification: str, catalog, *, clarifications: list[tuple[str, str]] | None = None) -> str:
    confirmed = ""
    if clarifications:
        pairs = "\n".join(f"Q: {question}\nA: {answer}" for question, answer in clarifications)
        confirmed = (
            "\nConfirmed clarifications below are decisions already made by the user. "
            "Do not repeat or reopen them. Ask one new question only when its answer is still "
            "necessary to define user-visible behavior, acceptance, or compatibility.\n"
            f"{pairs}\n"
        )
    return (
        "You are the read-only intake agent for a verified coding Goal.\n"
        "Read the repository only when needed. Identify only decisions the user must make; "
        "do not ask about facts discoverable from the repository.\n"
        f"Requested outcome: {target}\n"
        f"Proposed global verification: {verification or 'not inferred'}\n"
        f"{catalog.prompt_text()}\n"
        f"{confirmed}\n"
        "Reply ONLY as JSON: "
        '{"summary":"how you understand the requested outcome",'
        '"assumptions":["bounded assumption"],"questions":["short question"]}. '
        "Write the summary and questions in the user's language. Use an empty questions list when "
        "the requirement is sufficiently clear. Include only assumptions that materially constrain "
        "the result. Never propose implementation."
    )


def _pause_intake(draft: GoalDraft, root: Path, error: str, message: str) -> None:
    draft.status = "paused"
    draft.last_error = error[:4_000]
    _set_stage(draft, "paused")
    save_draft(draft, root, event="failed", message=message)


def _run_followup_intake(draft: GoalDraft, root: Path, catalog, *, intake_runner=None) -> None:
    """Re-check intake after a completed clarification round.

    A follow-up never replaces the previous Q/A history.  It either appends a
    new unresolved decision or returns no questions, which is the only signal
    that allows Discovery and planning to begin.
    """
    project_root = draft_project_root(draft, root)
    operation_timeout = max(1, int(draft.limits.get("operation_timeout_seconds", 1800)))
    clarifications = list(zip(draft.questions, draft.answers))
    prompt = _intake_prompt(draft.target, draft.verification, catalog, clarifications=clarifications)
    draft.status = "discovering"  # Prevent a second chat message being consumed while intake is running.
    _set_stage(draft, "intake", deadline=time.time() + operation_timeout)
    save_draft(draft, root, event="stage_started", message="Rechecking whether more clarification is needed")

    runner = intake_runner
    try:
        if runner is None:
            from harness.agents.runner import AgentTaskStats, run_agent_task

            stats = AgentTaskStats()
            with _draft_heartbeat(draft, root):
                raw = run_agent_task(
                    description="continue Goal clarification",
                    prompt=prompt,
                    agent_type="goal_intake",
                    cwd=str(project_root),
                    max_rounds=1,
                    tools_override=(),
                    stats=stats,
                    deadline=time.monotonic() + operation_timeout,
                )
            unavailable = (
                raw.startswith("[goal_intake] failed:")
                or raw.startswith("[goal_intake] stopped:")
                or stats.stop_reason in {"provider_error", "configuration_error", "empty_response"}
            )
            if unavailable:
                raise GoalDraftError(f"Goal intake is unavailable: {raw}")
        else:
            try:
                with _draft_heartbeat(draft, root):
                    raw = runner(
                        target=draft.target,
                        verification=draft.verification,
                        catalog=catalog,
                        clarifications=clarifications,
                        follow_up=True,
                        deadline=time.monotonic() + operation_timeout,
                    )
            except TypeError as exc:
                if not any(name in str(exc) for name in ("deadline", "clarifications", "follow_up")):
                    raise
                with _draft_heartbeat(draft, root):
                    raw = runner(target=draft.target, verification=draft.verification, catalog=catalog)
        result = _intake_from_raw(raw)
        _append_followup_intake_result(draft, result)
    except Exception as exc:
        message = "Goal follow-up intake failed"
        error = str(exc) if isinstance(exc, GoalDraftError) else f"Goal intake request failed: {type(exc).__name__}: {exc}"
        _pause_intake(draft, root, error, message)
        raise GoalDraftError(draft.last_error or error) from exc


def _planner_target(draft: GoalDraft) -> str:
    if not draft.answers:
        return draft.target
    pairs = "\n".join(f"Q: {question}\nA: {answer}" for question, answer in zip(draft.questions, draft.answers))
    return f"{draft.target}\n\nConfirmed clarifications:\n{pairs}"


def _plan(draft: GoalDraft, workspace: Path, catalog, planner_runner=None, reviewer_runner=None, discovery_manifest=None, verification_adapter=None) -> None:
    readiness_error = discovery_readiness_error(discovery_manifest)
    if readiness_error:
        raise GoalDraftError(readiness_error)
    plan = plan_tasks(
        _planner_target(draft),
        draft.verification,
        workspace,
        planner_runner=planner_runner,
        reviewer_runner=reviewer_runner,
        test_catalog=catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
        human_language=human_language_label(draft.language),
    )
    draft.goal_contract = dict(plan.contract)
    draft.goal_contract["language"] = draft.language
    draft.planning_review = dict(plan.review)
    draft.task_plan = [task.to_dict() for task in plan.tasks]
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
    if existing and existing.status not in {"consumed", "cancelled"}:
        raise GoalDraftError("A Goal draft already exists. Use /goal preview, answer, revise, approve, or discard.")
    project_root = resolve_target_project(root, target)
    command, source = infer_verification(project_root, verification)
    operation_timeout = max(1, int((limits or {}).get("operation_timeout_seconds", 1800)))
    draft_id = f"goal_draft_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    draft = GoalDraft(
        id=draft_id,
        target=target.strip(),
        verification=command,
        verification_source=source,
        status="clarifying",
        project_root=project_root.relative_to(root).as_posix() or ".",
        language=detect_goal_language(target),
        limits=dict(limits or {}),
        test_catalog_count=0,
        stage="preflight",
        input_hash=_input_hash(target.strip(), command, limits),
        stage_deadline=time.time() + operation_timeout,
        discovery_path=f".project/goal-memory/{draft_id}/discovery",
    )
    # Persist a recovery point before test collection or any model request.
    # The catalog is filled below after this write.
    draft.test_catalog_count = 0
    save_draft(draft, root, event="started", message="Goal draft persisted; running static preflight")
    try:
        _preflight_or_raise(
            draft,
            root,
            _required_draft_agents(
                intake_runner=intake_runner,
                planner_runner=planner_runner,
                discovery_runner=discovery_runner,
            ),
        )
    except GoalDraftError:
        raise
    try:
        _set_stage(draft, "catalog")
        save_draft(draft, root, event="stage_started", message="Collecting verification catalog")
        with _draft_heartbeat(draft, root):
            adapter, catalog = _collect_catalog(project_root, command)
        draft.verification_adapter = getattr(adapter, "id", "pytest")
        draft.test_catalog_count = len(catalog.selectors)
        _set_stage(draft, "intake")
        save_draft(draft, root, event="stage_started", message="Collecting requirement clarifications")
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"test catalog failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Verification catalog failed")
        raise GoalDraftError(draft.last_error) from exc
    if not command:
        draft.questions = ["没有找到可靠的全局回归命令。请告诉我最终应运行的测试命令。"]
        draft.intake_summary = "缺少可验证 Goal 所需的全局回归命令。"
    else:
        runner = intake_runner
        if runner is None:
            from harness.agents.runner import AgentTaskStats, run_agent_task

            stats = AgentTaskStats()
            try:
                with _draft_heartbeat(draft, root):
                    intake_prompt = _intake_prompt(draft.target, command, catalog)
                    raw = run_agent_task(
                        description="clarify verified coding goal",
                        prompt=intake_prompt,
                        agent_type="goal_intake",
                        cwd=str(project_root),
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
                save_draft(draft, root, event="failed", message="Goal intake request failed")
                raise GoalDraftError(draft.last_error) from exc
            if (
                raw.startswith("[goal_intake] failed:")
                or raw.startswith("[goal_intake] stopped:")
                or stats.stop_reason in {"provider_error", "configuration_error", "empty_response"}
            ):
                draft.status = "paused"
                draft.last_error = f"Goal intake is unavailable: {raw}"
                _set_stage(draft, "paused")
                save_draft(draft, root, event="failed", message="Goal intake provider unavailable")
                raise GoalDraftError(draft.last_error)
        else:
            try:
                with _draft_heartbeat(draft, root):
                    raw = runner(target=draft.target, verification=command, catalog=catalog, deadline=time.monotonic() + operation_timeout)
            except TypeError as exc:
                # Keep the small injectable test/provider contract usable while
                # the built-in runner receives the deadline.
                if "deadline" not in str(exc):
                    raise
                with _draft_heartbeat(draft, root):
                    raw = runner(target=draft.target, verification=command, catalog=catalog)
        intake_result: GoalIntakeResult | None = None
        parse_error: GoalDraftError | None = None
        try:
            intake_result = _intake_from_raw(raw)
        except GoalDraftError as first_error:
            parse_error = first_error
            # A reasoning model can finish with an empty/non-JSON visible
            # message while the provider request itself succeeded. Give it one
            # bounded correction turn before pausing the Draft.
            if runner is None:
                retry_stats = AgentTaskStats()
                retry_prompt = (
                    intake_prompt
                    + "\n\nYour previous response was not parseable. Reply with ONLY one JSON object "
                    + '{"summary":"...","assumptions":[],"questions":["..."]}. '
                    + "No markdown, explanation, or hidden reasoning-only answer."
                )
                with _draft_heartbeat(draft, root):
                    retry_raw = run_agent_task(
                        description="retry Goal intake as strict JSON",
                        prompt=retry_prompt,
                        agent_type="goal_intake",
                        cwd=str(root), max_rounds=1, tools_override=(),
                        stats=retry_stats,
                        deadline=time.monotonic() + operation_timeout,
                    )
                if (
                    retry_raw.startswith("[goal_intake] failed:")
                    or retry_raw.startswith("[goal_intake] stopped:")
                    or retry_stats.stop_reason in {"provider_error", "configuration_error", "empty_response"}
                ):
                    draft.status = "paused"
                    draft.last_error = f"Goal intake is unavailable: {retry_raw}"
                    _set_stage(draft, "paused")
                    save_draft(draft, root, event="failed", message="Goal intake provider unavailable")
                    raise GoalDraftError(draft.last_error)
                try:
                    intake_result = _intake_from_raw(retry_raw)
                except GoalDraftError as retry_error:
                    parse_error = retry_error
            if intake_result is None and (not isinstance(raw, str) or not raw.strip()):
                parse_error = GoalDraftError(
                    "Goal intake returned no visible JSON response after one retry; "
                    "the Pro provider returned an empty assistant message."
                )
        if intake_result is None:
            draft.status = "paused"
            draft.last_error = str(parse_error or "Goal intake returned invalid JSON")
            _set_stage(draft, "paused")
            save_draft(draft, root, event="failed", message="Goal intake returned invalid JSON")
            raise parse_error or GoalDraftError(draft.last_error)
        _apply_intake_result(draft, intake_result)
    if draft.questions:
        _set_stage(draft, "clarifying")
        save_draft(draft, root, event="completed", message="Waiting for clarification")
    if not draft.questions:
        # Make the transition explicit: an empty *questions list* is valid and
        # means intake found no user decision to ask. It is different from an
        # empty assistant response, which is rejected above.
        save_draft(draft, root, event="stage_started", message="Goal intake complete; no clarification needed")
        discovery_manifest = None
        if planner_runner is None:
            draft.status = "discovering"
            _set_stage(draft, "discovering")
            save_draft(draft, root, event="stage_started", message="Running repository discovery")
            try:
                if discovery_runner is None:
                    from harness.goal.discovery import DiscoverySupervisor

                    with _draft_heartbeat(draft, root):
                        discovery_manifest = DiscoverySupervisor().run(
                            goal_id=draft.id,
                            target=draft.target,
                            workspace=project_root,
                            storage_workspace=root,
                            deadline=time.monotonic() + operation_timeout,
                            human_language=human_language_label(draft.language),
                        ).to_dict()
                else:
                    with _draft_heartbeat(draft, root):
                        discovery_manifest = discovery_runner(draft=draft, workspace=project_root)
                failures = _manifest_failures(discovery_manifest or {})
                if failures and not _manifest_successes(discovery_manifest or {}):
                    draft.status = "paused"
                    draft.last_error = _format_discovery_failures(failures)
                    _set_stage(draft, "paused")
                    save_draft(draft, root, event="failed", message="Discovery returned failed jobs")
                    raise GoalDraftError(draft.last_error)
                draft.discovery_path = f".project/goal-memory/{draft.id}/discovery/manifest.json"
            except Exception as exc:
                draft.status = "paused"
                draft.last_error = f"Goal discovery failed: {type(exc).__name__}: {exc}"
                _set_stage(draft, "paused")
                if not isinstance(exc, GoalDraftError):
                    save_draft(draft, root, event="failed", message="Goal discovery failed")
                raise GoalDraftError(draft.last_error) from exc
        draft.status = "planning"
        _set_stage(draft, "planning")
        save_draft(draft, root, event="stage_started", message="Planning verifiable tasks")
        try:
            with _draft_heartbeat(draft, root):
                _plan(draft, project_root, catalog, planner_runner=planner_runner, discovery_manifest=discovery_manifest, verification_adapter=adapter)
        except Exception as exc:
            draft.status = "paused"
            draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
            _set_stage(draft, "paused")
            save_draft(draft, root, event="failed", message="Goal planning failed")
            raise GoalDraftError(draft.last_error) from exc
    save_draft(draft, root, event="completed", message="Goal draft is ready" if draft.status == "ready" else None)
    return draft


def _run_discovery_and_plan(draft: GoalDraft, root: Path, catalog, adapter, *, planner_runner=None, reviewer_runner=None, discovery_runner=None) -> None:
    operation_timeout = max(1, int(draft.limits.get("operation_timeout_seconds", 1800)))
    project_root = draft_project_root(draft, root)
    _preflight_or_raise(draft, root, _required_resume_agents(planner_runner=planner_runner, discovery_runner=discovery_runner))
    # A retry starts a new provider/discovery attempt. Do not keep presenting
    # the previous failure while heartbeat events are emitted.
    draft.last_error = None
    draft.status = "discovering"
    _set_stage(draft, "discovering", deadline=time.time() + operation_timeout)
    save_draft(draft, root, event="stage_started", message="Running repository discovery")
    try:
        if discovery_runner is None:
            from harness.goal.discovery import DiscoverySupervisor

            with _draft_heartbeat(draft, root):
                manifest = DiscoverySupervisor().run(
                    goal_id=draft.id, target=_planner_target(draft), workspace=project_root,
                    storage_workspace=root,
                    deadline=time.monotonic() + operation_timeout,
                    human_language=human_language_label(draft.language),
                ).to_dict()
        else:
            with _draft_heartbeat(draft, root):
                manifest = discovery_runner(draft=draft, workspace=project_root)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"Goal discovery failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Goal discovery failed")
        raise GoalDraftError(draft.last_error) from exc
    failed_jobs = _manifest_failures(manifest or {})
    if failed_jobs and not _manifest_successes(manifest or {}):
        draft.status = "paused"
        draft.last_error = _format_discovery_failures(failed_jobs)
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Discovery returned failed jobs")
        raise GoalDraftError(draft.last_error)
    draft.discovery_path = f".project/goal-memory/{draft.id}/discovery/manifest.json"
    draft.last_error = None
    draft.status = "planning"
    _set_stage(draft, "planning", deadline=time.time() + operation_timeout)
    save_draft(draft, root, event="stage_started", message="Planning verifiable tasks")
    try:
        with _draft_heartbeat(draft, root):
            _plan(draft, project_root, catalog, planner_runner=planner_runner, discovery_manifest=manifest, verification_adapter=adapter)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Goal planning failed")
        raise GoalDraftError(draft.last_error) from exc


def _run_plan_from_manifest(
    draft: GoalDraft,
    root: Path,
    catalog,
    adapter,
    manifest: dict[str, Any],
    *,
    planner_runner=None,
) -> None:
    """Resume a Planner-only failure without paying for Discovery again."""
    operation_timeout = max(1, int(draft.limits.get("operation_timeout_seconds", 1800)))
    project_root = draft_project_root(draft, root)
    _preflight_or_raise(
        draft,
        root,
        _required_resume_agents(planner_runner=planner_runner, include_discovery=False),
    )
    draft.last_error = None
    draft.status = "planning"
    _set_stage(draft, "planning", deadline=time.time() + operation_timeout)
    save_draft(draft, root, event="stage_started", message="Resuming task planning from saved discovery evidence")
    try:
        with _draft_heartbeat(draft, root):
            _plan(draft, project_root, catalog, planner_runner=planner_runner, reviewer_runner=reviewer_runner, discovery_manifest=manifest, verification_adapter=adapter)
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Goal planning failed")
        raise GoalDraftError(draft.last_error) from exc


def answer_draft(
    answer: str,
    *,
    workspace: Path | None = None,
    intake_runner: Callable[..., str] | None = None,
    planner_runner=None,
    discovery_runner=None,
) -> GoalDraft:
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
    # When the previous intake response already contains another question,
    # show it immediately. Catalog collection and model preflight are not
    # needed for a local Q/A transition and make the conversation feel stuck.
    if draft.unanswered_question is not None:
        draft.status = "clarifying"
        _set_stage(draft, "clarifying")
        save_draft(draft, root, event="completed", message="Waiting for the next clarification")
        return draft

    # Persist the final answer before the follow-up model call.  A second
    # composer message must not be interpreted as an answer while that call is
    # deciding whether another product decision is needed.
    draft.status = "discovering"
    _set_stage(draft, "catalog")
    save_draft(draft, root, event="stage_started", message="Clarification recorded; refreshing verification catalog")
    try:
        # A follow-up clarification only depends on the intake provider. Do
        # not reject a still-useful user question because a later Discovery or
        # Planner route is unavailable; those routes are preflighted when the
        # user decision is actually complete.
        _preflight_or_raise(draft, root, ("goal_intake",) if intake_runner is None else ())
        with _draft_heartbeat(draft, root):
            adapter, catalog = _collect_catalog(draft_project_root(draft, root), draft.verification)
    except GoalDraftError:
        raise
    except Exception as exc:
        draft.status = "paused"
        draft.last_error = f"test catalog failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Verification catalog failed")
        raise GoalDraftError(draft.last_error) from exc
    draft.verification_adapter = getattr(adapter, "id", "pytest")
    draft.test_catalog_count = len(catalog.selectors)
    _run_followup_intake(draft, root, catalog, intake_runner=intake_runner)
    if draft.unanswered_question is not None:
        draft.status = "clarifying"
        _set_stage(draft, "clarifying")
        save_draft(draft, root, event="completed", message="Waiting for follow-up clarification")
        return draft
    # Test/provider injectors may supply only a planner. Production always
    # takes the Discovery path; an injected discovery runner exercises the
    # same route without a live model request.
    if planner_runner is None or discovery_runner is not None:
        _run_discovery_and_plan(draft, root, catalog, adapter, planner_runner=planner_runner, discovery_runner=discovery_runner)
    else:
        _preflight_or_raise(
            draft,
            root,
            _required_resume_agents(planner_runner=planner_runner, include_discovery=False),
        )
        _set_stage(draft, "planning")
        save_draft(draft, root, event="stage_started", message="Planning verifiable tasks")
        try:
            with _draft_heartbeat(draft, root):
                _plan(draft, draft_project_root(draft, root), catalog, planner_runner, verification_adapter=adapter)
        except Exception as exc:
            draft.status = "paused"
            draft.last_error = f"Goal planning failed: {type(exc).__name__}: {exc}"
            _set_stage(draft, "paused")
            save_draft(draft, root, event="failed", message="Goal planning failed")
            raise GoalDraftError(draft.last_error) from exc
    save_draft(draft, root, event="completed" if draft.status == "ready" else "updated")
    return draft


def resume_draft(*, workspace: Path | None = None, planner_runner=None, discovery_runner=None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is not None and draft.stage in _ACTIVE_DRAFT_STAGES:
        # A frontend/backend restart can leave a Draft at a live stage after
        # its worker process has gone away. Treat a stale heartbeat as a
        # recoverable pause, but never steal a fresh operation.
        age = max(0.0, time.time() - draft.last_heartbeat)
        if age > 15:
            draft.status = "paused"
            draft.last_error = f"Previous Goal draft operation stopped after {int(age)}s without a heartbeat."
            _set_stage(draft, "paused")
            save_draft(draft, root, event="failed", message="Recovered interrupted Goal draft")
    if draft is None or draft.status != "paused":
        raise GoalDraftError("No paused Goal draft to resume.")
    from harness.agent.cancel import clear_cancel

    clear_cancel()
    resume_from = draft.resume_from or _legacy_resume_stage(draft.to_dict())
    retry_intake = resume_from == "intake" or (draft.last_error or "").lower().startswith("goal intake")
    if draft.unanswered_question:
        draft.status = "clarifying"
        draft.last_error = None
        _set_stage(draft, "clarifying")
        # The stored pause is the state being resumed, not a newer control
        # request. Persist this transition before any slow work begins.
        save_draft(draft, root, event="stage_started", message="Resuming Goal clarification", preserve_control=False)
        return draft
    draft.status = "discovering"
    draft.last_error = None
    _set_stage(draft, "catalog")
    # ``save_draft`` normally protects a pause/cancel written while an older
    # model call was in flight.  Here that durable pause is precisely what the
    # user asked to resume, so it must not overwrite the new active stage.
    save_draft(draft, root, event="stage_started", message="Resuming Goal draft; refreshing verification catalog", preserve_control=False)
    try:
        with _draft_heartbeat(draft, root):
            adapter, catalog = _collect_catalog(draft_project_root(draft, root), draft.verification)
    except Exception as exc:
        draft.last_error = f"test catalog failed: {type(exc).__name__}: {exc}"
        _set_stage(draft, "paused")
        save_draft(draft, root, event="failed", message="Verification catalog failed")
        raise GoalDraftError(draft.last_error) from exc
    draft.verification_adapter = getattr(adapter, "id", "pytest")
    draft.test_catalog_count = len(catalog.selectors)
    # Intake failures are retryable. The old implementation always resumed at
    # Discovery, leaving a Draft with no questions and skipping the failed
    # Pro intake stage entirely.
    if retry_intake:
        # A follow-up failure happens after at least one confirmed answer.
        # Re-run the same contextual check and append its result; replacing
        # ``questions`` here would erase the completed conversation.
        if draft.answers:
            _preflight_or_raise(draft, root, ("goal_intake",))
            _run_followup_intake(draft, root, catalog)
            if draft.unanswered_question:
                draft.status = "clarifying"
                _set_stage(draft, "clarifying")
                save_draft(draft, root, event="completed", message="Waiting for follow-up clarification")
                return draft
            draft.last_error = None
            retry_intake = False
        if retry_intake:
            from harness.agents.runner import AgentTaskStats, run_agent_task

            draft.status = "clarifying"
            draft.last_error = None
            _set_stage(draft, "intake")
            save_draft(draft, root, event="stage_started", message="Retrying Goal intake")
            stats = AgentTaskStats()
            with _draft_heartbeat(draft, root):
                raw = run_agent_task(
                    description="retry Goal intake as strict JSON",
                    prompt=(
                        _intake_prompt(draft.target, draft.verification, catalog)
                        + "\n\nReply with ONLY one JSON object "
                        + '{"summary":"...","assumptions":[],"questions":["..."]}.'
                    ),
                    agent_type="goal_intake", cwd=str(draft_project_root(draft, root)), max_rounds=1,
                    tools_override=(), stats=stats,
                    deadline=time.monotonic() + max(1, int(draft.limits.get("operation_timeout_seconds", 1800))),
                )
            if (
                raw.startswith("[goal_intake] failed:")
                or raw.startswith("[goal_intake] stopped:")
                or stats.stop_reason in {"provider_error", "configuration_error", "empty_response"}
            ):
                draft.status = "paused"
                draft.last_error = f"Goal intake is unavailable: {raw}"
                _set_stage(draft, "paused")
                save_draft(draft, root, event="failed", message="Goal intake provider unavailable")
                raise GoalDraftError(draft.last_error)
            try:
                _apply_intake_result(draft, _intake_from_raw(raw))
            except GoalDraftError as exc:
                draft.status = "paused"
                draft.last_error = str(exc)
                _set_stage(draft, "paused")
                save_draft(draft, root, event="failed", message="Goal intake retry returned invalid JSON")
                raise
            if draft.questions:
                _set_stage(draft, "clarifying")
                save_draft(draft, root, event="completed", message="Waiting for clarification")
                return draft
            draft.last_error = None
    if resume_from == "planning":
        from harness.goal.discovery_store import load_manifest

        manifest = load_manifest(root, draft.id)
        if _is_reusable_discovery_manifest(manifest):
            _run_plan_from_manifest(
                draft,
                root,
                catalog,
                adapter,
                manifest,
                planner_runner=planner_runner,
            )
            save_draft(draft, root, event="completed" if draft.status == "ready" else "updated")
            return draft
    _run_discovery_and_plan(draft, root, catalog, adapter, planner_runner=planner_runner, discovery_runner=discovery_runner)
    save_draft(draft, root, event="completed" if draft.status == "ready" else "updated")
    return draft


def approve_draft(*, workspace: Path | None = None) -> GoalDraft:
    root = (workspace or get_workdir()).resolve()
    draft = load_draft(root)
    if draft is None:
        raise GoalDraftError("No Goal draft to approve.")
    if (
        draft.status not in {"ready", "approved"}
        or not draft.task_plan
        or not draft.verification
        or not draft.goal_contract
        or draft.planning_review.get("approved") is not True
    ):
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
    if (
        draft.status not in {"ready", "approved"}
        or not draft.task_plan
        or not draft.verification
        or not draft.goal_contract
        or draft.planning_review.get("approved") is not True
    ):
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
        f"  Project root: {draft.project_root}",
        f"  Global verification: {draft.verification or 'needs your answer'} ({draft.verification_source})",
        f"  Verification adapter: {draft.verification_adapter}; collected selectors: {draft.test_catalog_count}",
        f"  Stage: {draft.stage} (heartbeat {int(max(0, time.time() - draft.last_heartbeat))}s ago)",
    ]
    if draft.intake_summary:
        lines.append(f"  Intake: {draft.intake_summary}")
    for assumption in draft.intake_assumptions:
        lines.append(f"    Assumption: {assumption}")
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
