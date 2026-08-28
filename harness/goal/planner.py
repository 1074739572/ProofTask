"""Goal decomposition planner with machine-checkable Task contracts.

The planning model describes Tasks and acceptance cases.  It may select only
pytest node IDs already discovered by the system; it never gets to invent a
test command and call that proof. Task plans are the only Goal execution
projection.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.settings import get_workdir
from harness.verification import VerificationContext, select_adapter
from harness.verification.catalog import TestCatalog, build_pytest_command, collect_pytest_catalog

PLANNER_AGENT = "goal_planner"
PLAN_REVIEWER_AGENT = "goal_plan_reviewer"
# Planning turns a confirmed requirement plus a machine-collected test catalog
# into a contract. Deep repository inspection belongs to the test writer and
# implementation worker; keeping planning tool-free prevents an invisible,
# unbounded explore loop before a draft can be saved.
PLANNER_MAX_ROUNDS = 1
PLANNER_MAX_OUTPUT_TOKENS = 12_000
PLANNER_ESCALATED_OUTPUT_TOKENS = 24_000
# The continuation is a new provider request. It must not inherit a nearly
# exhausted read deadline from the 12k attempt, otherwise the advertised 24k
# upgrade deterministically fails before the model can answer.
PLANNER_CONTINUATION_TIMEOUT_SECONDS = 600.0
# The configured Sol relay buffers the initial response, so the shared 90s
# read timeout is too short for a high-reasoning Goal contract. One longer
# attempt is more predictable than three full 90s retries.
PLANNER_READ_TIMEOUT_SECONDS = 300.0
PLANNER_MAX_REQUEST_ATTEMPTS = 1
PLANNER_FORMAT_RETRY_MAX_ROUNDS = 1
PLAN_REVIEW_MAX_ROUNDS = 1
PLANNER_REPAIR_INPUT_LIMIT = 24_000
MAX_BEHAVIOR_CHARS = 600
MAX_ACCEPTANCE_CASES = 8
MAX_SKILLS_PER_TASK = 2
PLANNER_CATALOG_LIMIT = 80

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_AGENT_HEADER = re.compile(r"^\[[^\]]+\] [^\n]*\n*")


class GoalPlanningError(ValueError):
    """Raised when a Goal cannot be decomposed into valid Task contracts."""

    def __init__(self, message: str, *, requires_discovery_refresh: bool = False):
        super().__init__(message)
        self.requires_discovery_refresh = requires_discovery_refresh


def _strip_agent_header(text: str) -> str:
    stripped = text.lstrip()
    if _AGENT_HEADER.match(stripped):
        stripped = _AGENT_HEADER.sub("", stripped, count=1)
    return stripped.strip()


def _normalise_strings(raw: Any, *, limit: int | None = None) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        text = value.strip().replace("\\", "/")
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text[:500])
        if limit is not None and len(values) >= limit:
            break
    return tuple(values)


def _valid_scope_path(path: str) -> bool:
    """Keep planner scope declarations relative to the workspace."""
    candidate = Path(path)
    return not candidate.is_absolute() and ".." not in candidate.parts


@dataclass(frozen=True)
class AcceptanceCase:
    """One observable behavior required for a Task to be accepted."""

    id: str
    given: str
    when: str
    then: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "given": self.given, "when": self.when, "then": self.then}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int) -> "AcceptanceCase | None":
        case_id = str(data.get("id") or f"AC{index}").strip()[:40]
        given = str(data.get("given") or "").strip()[:500]
        when = str(data.get("when") or "").strip()[:500]
        then = str(data.get("then") or data.get("expected") or "").strip()[:500]
        if not case_id or not then:
            return None
        return cls(id=case_id, given=given, when=when, then=then)


@dataclass(frozen=True)
class VerificationSpec:
    """A Task's evidence contract, produced and validated by the system."""

    adapter: str = "command"
    command: str = ""
    test_files: tuple[str, ...] = ()
    selectors: tuple[str, ...] = ()
    source: str = "needs_generation"  # user | legacy | discovered | generated | needs_generation
    collected_count: int = 0
    baseline_result: str = "not_run"
    confidence: str = "low"
    # Immutable proof metadata captured by the machine when a binding is made.
    # The model never supplies these fields.
    baseline_evidence: dict[str, Any] = field(default_factory=dict)
    test_hashes: dict[str, str] = field(default_factory=dict)
    covers: tuple[str, ...] = ()
    # A focused test normally belongs to one Task. Integration coverage is
    # intentionally shared and must name every owning Task.
    owners: tuple[str, ...] = ()
    case_selectors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "command": self.command,
            "test_files": list(self.test_files),
            "selectors": list(self.selectors),
            "source": self.source,
            "collected_count": self.collected_count,
            "baseline_result": self.baseline_result,
            "confidence": self.confidence,
            "baseline_evidence": dict(self.baseline_evidence),
            "test_hashes": dict(self.test_hashes),
            "covers": list(self.covers),
            "owners": list(self.owners),
            "case_selectors": {key: list(value) for key, value in self.case_selectors.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "VerificationSpec":
        # Older/manual state may contain malformed optional proof metadata.
        # Do not make an otherwise recoverable Goal impossible to load.
        data = data if isinstance(data, dict) else {}
        try:
            collected_count = max(0, int(data.get("collected_count") or 0))
        except (TypeError, ValueError):
            collected_count = 0
        return cls(
            adapter=str(data.get("adapter") or "command")[:40],
            command=str(data.get("command") or "").strip()[:1000],
            test_files=_normalise_strings(data.get("test_files")),
            selectors=_normalise_strings(data.get("selectors")),
            source=str(data.get("source") or "needs_generation")[:40],
            collected_count=collected_count,
            baseline_result=str(data.get("baseline_result") or "not_run")[:80],
            confidence=str(data.get("confidence") or "low")[:20],
            baseline_evidence=(
                dict(data.get("baseline_evidence"))
                if isinstance(data.get("baseline_evidence"), dict)
                else {}
            ),
            test_hashes={str(k): str(v) for k, v in (data.get("test_hashes") or {}).items()}
            if isinstance(data.get("test_hashes"), dict)
            else {},
            covers=_normalise_strings(data.get("covers")),
            owners=_normalise_strings(data.get("owners")),
            case_selectors={
                str(case): _normalise_strings(selectors)
                for case, selectors in (data.get("case_selectors") or {}).items()
                if isinstance(case, str) and isinstance(selectors, (list, tuple))
            } if isinstance(data.get("case_selectors"), dict) else {},
        )


@dataclass(frozen=True)
class TaskPlan:
    """A durable planning unit with machine-verifiable acceptance criteria."""

    name: str
    behavior: str
    depends_on: tuple[str, ...] = ()
    acceptance_cases: tuple[AcceptanceCase, ...] = ()
    skill_names: tuple[str, ...] = ()
    verification_spec: VerificationSpec = field(default_factory=VerificationSpec)
    primary_write: tuple[str, ...] = ()
    planned_new: tuple[str, ...] = ()
    conditional_write: tuple[str, ...] = ()
    read_envelope: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    test_strategy: str = ""
    discovery_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "behavior": self.behavior,
            "depends_on": list(self.depends_on),
            "acceptance_cases": [case.to_dict() for case in self.acceptance_cases],
            "skills": list(self.skill_names),
            "verification_spec": self.verification_spec.to_dict(),
            "primary_write": list(self.primary_write),
            "planned_new": list(self.planned_new),
            "conditional_write": list(self.conditional_write),
            "read_envelope": list(self.read_envelope),
            "forbidden": list(self.forbidden),
            "evidence_refs": list(self.evidence_refs),
            "test_strategy": self.test_strategy,
            "discovery_revision": self.discovery_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPlan":
        cases = _parse_acceptance_cases(data.get("acceptance_cases"))
        spec_raw = data.get("verification_spec")
        return cls(
            name=str(data["name"]),
            behavior=str(data["behavior"]),
            depends_on=tuple(str(dep) for dep in (data.get("depends_on") or ())),
            acceptance_cases=cases,
            skill_names=_normalise_skill_names(data.get("skills")),
            verification_spec=VerificationSpec.from_dict(spec_raw),
            primary_write=_normalise_strings(data.get("primary_write")),
            planned_new=_normalise_strings(data.get("planned_new")),
            conditional_write=_normalise_strings(data.get("conditional_write")),
            read_envelope=_normalise_strings(data.get("read_envelope")),
            forbidden=_normalise_strings(data.get("forbidden")),
            evidence_refs=_normalise_strings(data.get("evidence_refs")),
            test_strategy=str(data.get("test_strategy") or "")[:1000],
            discovery_revision=max(0, int(data.get("discovery_revision") or 0)),
        )


@dataclass(frozen=True)
class GoalPlan:
    """Planning v2 output: a decision contract plus executable Task contracts."""

    contract: dict[str, Any]
    tasks: tuple[TaskPlan, ...]
    review: dict[str, Any] = field(default_factory=dict)


def _parse_acceptance_cases(raw: Any) -> tuple[AcceptanceCase, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ACCEPTANCE_CASES:
        return ()
    cases: list[AcceptanceCase] = []
    ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return ()
        case = AcceptanceCase.from_dict(item, index=index)
        if case is None or case.id in ids:
            return ()
        ids.add(case.id)
        cases.append(case)
    return tuple(cases)


def _normalise_skill_names(raw: Any) -> tuple[str, ...]:
    """Keep only installed, bounded skill ids from planner-supplied JSON."""
    from harness.skills_loader import skill_names

    installed = set(skill_names())
    return tuple(name for name in _normalise_strings(raw, limit=MAX_SKILLS_PER_TASK) if name in installed)


def _recommended_skill_names(name: str, behavior: str, spec: VerificationSpec) -> tuple[str, ...]:
    """Assign safe defaults when the planner omits clearly relevant guidance."""
    from harness.skills_loader import skill_names as installed_skill_names

    text = f"{name} {behavior}".lower()
    installed = set(installed_skill_names())
    ui_markers = ("frontend", "front-end", " ui", "ux", "react", "vue", "css", "html", "page", "component", "前端", "界面", "页面", "组件", "动画")
    repair_markers = ("bug", "fix", "repair", "regression", "failure", "error", "debug", "修复", "错误", "回归", "故障")
    if any(marker in text for marker in ui_markers):
        return tuple(name for name in ("frontend-design", "webapp-testing") if name in installed)
    if any(marker in text for marker in repair_markers):
        return tuple(name for name in ("systematic-debugging",) if name in installed)
    if spec.source == "needs_generation" and "test-driven-development" in installed:
        return ("test-driven-development",)
    return ()


def _planner_manifest_view(discovery_manifest: dict[str, Any]) -> dict[str, Any]:
    """Keep planning evidence visible without sending the repository map.

    The full manifest is deliberately retained for ``parse_plan`` to validate
    model output.  Its ``repo_files`` and ``shards`` fields can be megabytes,
    though, and putting them first in a character-truncated prompt hides the
    evidence the planner actually needs.
    """
    evidence: list[dict[str, Any]] = []
    scope_candidates: list[str] = []
    seen_scopes: set[str] = set()

    def add_scope(path: Any) -> None:
        normalized = str(path or "").strip().replace("\\", "/").strip("/")
        if not normalized or normalized in seen_scopes:
            return
        seen_scopes.add(normalized)
        scope_candidates.append(normalized)

    for raw in discovery_manifest.get("evidence", []) if isinstance(discovery_manifest.get("evidence"), list) else []:
        if not isinstance(raw, dict):
            continue
        evidence_id = str(raw.get("id") or "").strip()
        path = str(raw.get("path") or "").strip().replace("\\", "/")
        claim = str(raw.get("claim") or "").strip()
        if not evidence_id or not path or not claim:
            continue
        lines = raw.get("lines")
        item: dict[str, Any] = {
            "id": evidence_id[:80],
            "path": path[:500],
            "claim": claim[:800],
            "symbol": str(raw.get("symbol") or "").strip()[:160],
            "source_job": str(raw.get("source_job") or "").strip()[:80],
        }
        if isinstance(lines, (list, tuple)) and len(lines) == 2:
            item["lines"] = list(lines)
        evidence.append(item)
        add_scope(path)
        parts = path.split("/")
        for index in range(1, len(parts)):
            add_scope("/".join(parts[:index]))

    jobs: list[dict[str, str]] = []
    for raw in discovery_manifest.get("jobs", []) if isinstance(discovery_manifest.get("jobs"), list) else []:
        if not isinstance(raw, dict):
            continue
        jobs.append({
            "id": str(raw.get("id") or "")[:80],
            "role": str(raw.get("role") or "")[:80],
            "status": str(raw.get("status") or "")[:40],
            "error": str(raw.get("error") or "")[:300],
        })

    return {
        "base_revision": str(discovery_manifest.get("base_revision") or "unknown")[:120],
        "revision": discovery_manifest.get("revision", 0),
        "evidence": evidence,
        "scope_candidates": scope_candidates,
        "implementation_candidates": list(_implementation_candidates(discovery_manifest)),
        "gaps": [str(item)[:500] for item in discovery_manifest.get("gaps", []) if str(item).strip()],
        "conflicts": [str(item)[:500] for item in discovery_manifest.get("conflicts", []) if str(item).strip()],
        "jobs": jobs,
    }


_SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".cs"})
_EVIDENCE_PATH_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|rb|php|cs)(?![\w/-])", re.IGNORECASE)
_PYTHON_MODULE_RE = re.compile(r"(?<![\w.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,}(?!\w)")


def _implementation_candidates(discovery_manifest: dict[str, Any]) -> tuple[str, ...]:
    """Resolve source entry points named by Discovery reports to exact repo files.

    A Discovery report often documents an extension point as ``hooks.py`` or
    ``cli.py`` while the machine-collected repository map owns the full path.
    Preserve that link for planning instead of forcing a second Discovery pass
    merely to turn a known basename into an exact file scope.
    """
    source_paths = tuple(
        str(path or "").replace("\\", "/").strip()
        for path in discovery_manifest.get("repo_files", [])
        if Path(str(path or "")).suffix.lower() in _SOURCE_SUFFIXES
    )
    by_name: dict[str, list[str]] = {}
    for path in source_paths:
        by_name.setdefault(Path(path).name.lower(), []).append(path)

    candidates: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            candidates.append(path)

    for item in discovery_manifest.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence_path = str(item.get("path") or "").replace("\\", "/").strip()
        if Path(evidence_path).suffix.lower() in _SOURCE_SUFFIXES and evidence_path in source_paths:
            add(evidence_path)
        claim = str(item.get("claim") or "")
        for mention in _EVIDENCE_PATH_RE.findall(claim):
            normalized = mention.replace("\\", "/")
            if normalized in source_paths:
                add(normalized)
                continue
            matches = by_name.get(Path(normalized).name.lower(), [])
            if len(matches) == 1:
                add(matches[0])
        # Requirement documents commonly name Python extension points in
        # dotted-module form (for example ``harness.agents.runner``). Resolve
        # them only when that exact module maps to an indexed source file.
        for module in _PYTHON_MODULE_RE.findall(claim):
            candidate = module.replace(".", "/") + ".py"
            if candidate in source_paths:
                add(candidate)
    return tuple(candidates[:80])


def discovery_readiness_error(discovery_manifest: dict[str, Any] | None) -> str | None:
    """Reject planning only when system-validated evidence is insufficient.

    Agent-reported gaps are advisory prose. They can describe role-local
    assignments, external reference projects, or uncertainty, so parsing them
    as a global gate makes planning depend on wording instead of facts.
    """
    if not isinstance(discovery_manifest, dict):
        return None
    evidence = [item for item in discovery_manifest.get("evidence", []) if isinstance(item, dict)]
    if not evidence:
        return "Discovery produced no validated evidence. Continue discovery before planning."
    repo_source_paths = {
        str(path or "").replace("\\", "/").strip()
        for path in discovery_manifest.get("repo_files", [])
        if Path(str(path or "")).suffix.lower() in _SOURCE_SUFFIXES
    }
    source_evidence_paths = {
        str(item.get("path") or "").replace("\\", "/").strip()
        for item in evidence
        if Path(str(item.get("path") or "")).suffix.lower() in _SOURCE_SUFFIXES
    }
    if repo_source_paths and not source_evidence_paths and not _implementation_candidates(discovery_manifest):
        return "Discovery produced no validated source-code evidence. Continue discovery before planning."
    return None


def _task_has_source_evidence(
    scopes: tuple[str, ...], refs: tuple[str, ...], discovery_manifest: dict[str, Any],
) -> bool:
    evidence_by_id = {
        str(item.get("id")): str(item.get("path") or "").replace("\\", "/")
        for item in discovery_manifest.get("evidence", [])
        if isinstance(item, dict)
    }
    code_scopes = [scope for scope in scopes if Path(scope).suffix.lower() in _SOURCE_SUFFIXES]
    if not code_scopes:
        code_scopes = [
            scope for scope in scopes
            if any(Path(path).suffix.lower() in _SOURCE_SUFFIXES and (path == scope or path.startswith(scope.rstrip("/") + "/"))
                   for path in discovery_manifest.get("repo_files", []))
        ]
    if not code_scopes:
        return True
    for ref in refs:
        path = evidence_by_id.get(ref, "")
        if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in code_scopes):
            return True
    # The repository inventory is machine-collected.  A report that names a
    # unique entry-point basename can therefore safely authorize that exact
    # indexed file for planning, without silently rerunning Discovery.
    candidates = set(_implementation_candidates(discovery_manifest))
    return bool(candidates) and all(scope in candidates for scope in code_scopes)


def build_plan_prompt(
    target: str,
    full_verification: str,
    test_catalog: TestCatalog | None = None,
    discovery_manifest: dict[str, Any] | None = None,
    *,
    human_language: str = "English",
    frozen_contract: dict[str, Any] | None = None,
    completed_task_names: tuple[str, ...] = (),
    replan_reason: str = "",
    execution_workspace_paths: tuple[str, ...] = (),
) -> str:
    """Prompt for the read-only planner. Output is a GoalPlan v2 object."""
    catalog_text = (test_catalog or TestCatalog(error="not collected")).prompt_text(limit=PLANNER_CATALOG_LIMIT)
    manifest_text = "No Discovery Manifest supplied. Do not guess repository paths."
    if isinstance(discovery_manifest, dict):
        manifest_text = (
            "Discovery evidence view (machine-collected; cite evidence IDs exactly). "
            "The full repository map remains available only for local validation:\n"
            + json.dumps(_planner_manifest_view(discovery_manifest), ensure_ascii=False)
        )
    replan_context = ""
    if frozen_contract is not None:
        replan_context = (
            "\nThis is execution-time replanning, not a new product-planning pass. "
            "The frozen goal_contract below is authoritative: reproduce its six fields exactly and do not "
            "broaden its outcome, constraints, assumptions, or verification preconditions. Return only replacement "
            "Tasks for the affected unfinished work only. Retained Tasks are existing dependency anchors and must not be emitted again. "
            "A replacement Task may depend on one of these retained Task names: "
            f"{json.dumps(list(completed_task_names), ensure_ascii=False)}.\n"
            "Use the replan trigger below to correct task boundaries. Every acceptance case assigned "
            "to a Task must be satisfiable by that Task and its completed dependencies; do not assign "
            "a runtime integration acceptance case to an earlier Task when its execution route is a "
            "separate pending Task.\n"
            "The execution-workspace paths below are authoritative current facts, even when they differ "
            "from the original Discovery evidence. An existing listed path may be primary_write or read_envelope, "
            "but must never be planned_new. Do not emit a missing original path as a Task scope.\n"
            f"Current execution-workspace paths: {json.dumps(list(execution_workspace_paths), ensure_ascii=False)}\n"
            f"Replan trigger evidence:\n{replan_reason or '(none supplied)'}\n"
            f"Frozen goal_contract:\n{json.dumps(_contract_projection(frozen_contract), ensure_ascii=False)}\n\n"
        )
    return (
        "You are the Goal planning compiler. Convert repository evidence and a confirmed request into "
        "one decision contract plus independently executable Task contracts.\n"
        "Do not ask questions, inspect files, or call tools. Do not solve uncertainty by making a worker guess later.\n\n"
        f"Goal target: {target}\n"
        f"Goal-level full verification command: {full_verification}\n\n"
        "Test binding rules:\n"
        "- The catalog below was collected by the system. A selector is valid only if it "
        "appears there exactly.\n"
        "- Never emit a verification command. The system builds it from test_selectors.\n"
        "- Use existing test_selectors only when every acceptance case has an exact case_selectors mapping. "
        "Otherwise use an empty test_selectors array; the system will generate focused tests later.\n"
        "- During execution-time replanning, selectors already present in the machine catalog are evidence of behavior "
        "already proved by completed or recovered Goal work. Do not create a test-generation Task whose acceptance "
        "cases require those existing selectors. Either bind the existing selectors with an exact mapping for every "
        "case, or omit the already-proved cases and create a Task only for missing behavior. A new test writer must "
        "never be asked to reuse an existing selector as newly generated coverage.\n"
        "- Do not invent existing files, paths, commands, or selectors. A planned_new path may be a new "
        "file or new directory only when its parent is evidenced by the repository.\n\n"
        f"{catalog_text}\n\n"
        f"{manifest_text}\n\n"
        f"{replan_context}"
        "First compile goal_contract, then split it into Tasks:\n"
        "- goal_contract.summary states the intended outcome in one precise sentence.\n"
        "- constraints are non-negotiable product, safety, compatibility, and user decisions.\n"
        "- assumptions are bounded decisions inferred from evidence; never hide an unresolved product choice here.\n"
        "- unresolved must be an empty array before execution. If the request genuinely needs a user decision, return it here and no tasks.\n"
        "- verification_preconditions state external/runtime facts that must hold before a real integration test.\n"
        "- decision_ledger records important architecture choices as objects with id, decision, rationale, and evidence_refs.\n"
        "- Task scope has five explicit classes: primary_write (existing files to edit), planned_new (new files/directories), conditional_write (existing files that may be added only after proof), read_envelope (existing paths needed to understand the task), forbidden (paths that must not change).\n"
        "- Never put a whole project root or a broad parent directory in primary_write. Use the smallest exact existing files.\n"
        "- primary_write, conditional_write, and read_envelope must be discovered paths. planned_new must not already exist.\n\n"
        "State-lifetime rule:\n"
        "- Separate persistent implementation artifacts from runtime user selections. Source files and checked-in configuration may be primary_write when the Task explicitly implements a schema, shipped default policy, or parser for them.\n"
        "- A current-session, temporary, or non-persistent user selection belongs in session/runtime state. It must not be written back to a user/default configuration file on each command invocation.\n"
        "- When a Task writes a configuration file and the Goal has session-scoped behavior, its behavior and acceptance cases must explicitly say that the write creates static schema/default support while the runtime selection remains session-scoped. If it only reads the configuration, list it only in read_envelope.\n\n"
        "Integration-closure rule:\n"
        "- implementation_candidates are exact source files from the machine-collected repository map whose extension-point names were cited by Discovery. They are valid existing-file scope candidates; do not replace them with guessed paths.\n"
        "- For a feature that changes how a command affects later tool calls, the Tasks together must explicitly cover: command/input registration, current-session state ownership, and the centralized enforcement hook. A static configuration/schema task is separate from, and cannot substitute for, the runtime enforcement task.\n"
        "- Put every required integration boundary in primary_write or read_envelope. Do not hide command routing, session state, hook registration, or an explicitly requested client/event bridge only in broad behavior prose. Add a dependency when one boundary needs another.\n"
        "- Do not invent a UI or event-stream task when the confirmed Goal limits the feature to the ordinary CLI; include such a boundary only when the Goal or Discovery evidence requires it.\n\n"
        "Split the Goal into as many Tasks as its independently verifiable deliverables require:\n"
        "- First compare every requested behavior against Discovery evidence and classify it internally as implemented, partial, missing, or unknown. Create Tasks only for partial or missing behavior; implemented behavior may need regression coverage but is not implementation work.\n"
        "- There is no target Task count. Cover every distinct deliverable; never merge work merely to reduce the count.\n"
        "- Each Task is independently implementable and machine-verifiable.\n"
        "- Each Task must include 1-8 concrete acceptance_cases using given/when/then; split a Task that needs more.\n"
        "- depends_on lists names of earlier Tasks only.\n"
        "- Every Task needs a non-empty primary_write or planned_new list. The system derives evidence_refs from these paths.\n"
        "- test_strategy must explain how each acceptance case will be verified.\n"
        "- case_selectors maps each acceptance case id to exact catalog selectors; never claim coverage without a mapping.\n"
        "- Keep related implementation details together, but preserve independently testable deliverables as separate Tasks.\n"
        "- Use one Task only when the Goal cannot be meaningfully split.\n\n"
        "Optional workflow skills:\n"
        "- Add skills only when directly relevant, with at most two installed skill names per Task.\n"
        "- Prefer systematic-debugging for a repair or regression Task, frontend-design for UI work, "
        "webapp-testing for browser/UI verification, and test-driven-development for a behavior that needs focused tests.\n"
        "- Do not use code-review here; the independent evaluator handles review.\n\n"
        f"Write all human-readable Task names, behavior, acceptance-case text, and test_strategy in {human_language}. "
        "Keep JSON keys, evidence IDs, paths, commands, and selectors exactly as supplied.\n\n"
        "Reply with ONLY one JSON object in this schema:\n"
        '{"goal_contract":{"summary":"...","constraints":["..."],"assumptions":["..."],"unresolved":[],"verification_preconditions":["..."],"decision_ledger":[{"id":"D1","decision":"...","rationale":"...","evidence_refs":["E1"]}]},"tasks":['
        '{"name":"paginate list","behavior":"list returns every page",'
        '"acceptance_cases":[{"id":"AC1","given":"more than one page",'
        '"when":"the caller requests pages","then":"no row is skipped"}],'
        '"test_selectors":["tests/test_pagination.py::test_all_pages"],'
        '"depends_on":[],"primary_write":["src/list.py"],"planned_new":[],"conditional_write":[],"read_envelope":["src/list.py"],"forbidden":[".env"],"evidence_refs":["E1"],'
        '"test_strategy":"one selector per acceptance case", "case_selectors":{"AC1":["tests/test_pagination.py::test_all_pages"]},'
        '"skills":["test-driven-development"]}]}\n'
        "No prose and no code fence."
    )


def _extract_json_object(text: str) -> str | None:
    if not text or not text.strip():
        return None
    for candidate in _FENCE_RE.findall(text):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            return candidate
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _spec_from_entry(
    entry: dict[str, Any], catalog, cases: tuple[AcceptanceCase, ...], verification_adapter=None,
) -> VerificationSpec:
    spec_data = entry.get("verification_spec") if isinstance(entry.get("verification_spec"), dict) else {}
    requested = _normalise_strings(
        entry.get("test_selectors", spec_data.get("selectors")),
    )
    raw_mapping = entry.get("case_selectors", spec_data.get("case_selectors"))
    case_selectors = {
        str(case): _normalise_strings(values)
        for case, values in raw_mapping.items()
        if isinstance(raw_mapping, dict) and isinstance(case, str) and isinstance(values, (list, tuple))
    } if isinstance(raw_mapping, dict) else {}

    # Existing tests prove a Task only when every acceptance case is mapped to
    # a collected selector. A partial mapping is useful planning context, but
    # it is not proof: route that Task to focused test generation instead of
    # rejecting the complete Goal plan.
    mapping_is_complete = (
        bool(cases)
        and all(case_selectors.get(case.id) for case in cases)
        and all(
            selector in requested
            for selectors in case_selectors.values()
            for selector in selectors
        )
    )
    # Modern planner output is grounded only against an actual Test Catalog.
    if (
        catalog and catalog.available and requested
        and all(catalog.contains(item) for item in requested)
        and mapping_is_complete
    ):
        files = tuple(dict.fromkeys(item.split("::", 1)[0] for item in requested))
        adapter = verification_adapter
        adapter_id = getattr(adapter, "id", getattr(catalog, "adapter", "pytest"))
        command = adapter.build_command(requested) if adapter is not None else build_pytest_command(requested)
        return VerificationSpec(
            adapter=adapter_id,
            command=command,
            test_files=files,
            selectors=requested,
            source="discovered",
            collected_count=len(requested),
            baseline_result="not_run",
            confidence="high",
            covers=tuple(case.id for case in cases if case_selectors.get(case.id)),
            case_selectors=case_selectors,
        )
    adapter_id = getattr(verification_adapter, "id", getattr(catalog, "adapter", "pytest"))
    return VerificationSpec(adapter=adapter_id, source="needs_generation")


def _inferred_evidence_refs(scopes: tuple[str, ...], discovery_manifest: dict[str, Any]) -> tuple[str, ...]:
    """Select durable evidence for the Planner's declared workspace scope.

    Evidence ids are bookkeeping identifiers, not product decisions. Deriving
    them here avoids making a valid Task depend on a model copying ``E17``
    correctly while preserving the source-evidence gate below.
    """
    refs: list[str] = []
    for item in discovery_manifest.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id") or "").strip()
        path = str(item.get("path") or "").replace("\\", "/").strip()
        if not evidence_id or not path:
            continue
        if any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes):
            refs.append(evidence_id)
    return tuple(dict.fromkeys(refs))


def _contract_error_text(errors: list[str]) -> str:
    unique = list(dict.fromkeys(errors))[:24]
    return "Plan contract errors:\n" + "\n".join(f"- {error}" for error in unique)


def _path_is_existing(path: str, files: set[str]) -> bool:
    return path in files


def _path_is_existing_envelope(path: str, files: set[str]) -> bool:
    return any(item == path or item.startswith(path.rstrip("/") + "/") for item in files)


def _path_can_be_planned_new(path: str, files: set[str]) -> bool:
    if not _valid_scope_path(path) or _path_is_existing_envelope(path, files):
        return False
    parent = Path(path).parent.as_posix()
    return parent in {"", "."} or _path_is_existing_envelope(parent, files)


def planning_error_requires_discovery(error: str | None) -> bool:
    """Whether a rejected plan relies on paths the current manifest cannot prove."""
    text = str(error or "")
    return any(
        marker in text
        for marker in (
            "primary_write must contain exact discovered files",
            "conditional_write must contain exact discovered files",
            "read_envelope must contain discovered files or directories",
        )
    )


def _parse_goal_contract(raw: Any, evidence_ids: set[str]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["goal_contract must be a JSON object"]
    contract = {
        "summary": str(raw.get("summary") or "").strip()[:1000],
        "constraints": list(_normalise_strings(raw.get("constraints"), limit=24)),
        "assumptions": list(_normalise_strings(raw.get("assumptions"), limit=24)),
        "unresolved": list(_normalise_strings(raw.get("unresolved"), limit=12)),
        "verification_preconditions": list(_normalise_strings(raw.get("verification_preconditions"), limit=24)),
        "decision_ledger": [],
    }
    if not contract["summary"]:
        errors.append("goal_contract is missing summary")
    if contract["unresolved"]:
        errors.append("goal_contract has unresolved decisions: " + "; ".join(contract["unresolved"][:4]))
    ledger = raw.get("decision_ledger")
    if not isinstance(ledger, list) or not ledger:
        errors.append("goal_contract needs a non-empty decision_ledger")
    else:
        ids: set[str] = set()
        for index, item in enumerate(ledger, start=1):
            if not isinstance(item, dict):
                errors.append(f"decision_ledger entry {index} must be an object")
                continue
            decision_id = str(item.get("id") or "").strip()[:40]
            decision = str(item.get("decision") or "").strip()[:800]
            rationale = str(item.get("rationale") or "").strip()[:1000]
            refs = tuple(ref for ref in _normalise_strings(item.get("evidence_refs")) if ref in evidence_ids)
            if not decision_id or decision_id in ids or not decision or not rationale:
                errors.append(f"decision_ledger entry {index} needs unique id, decision, and rationale")
                continue
            ids.add(decision_id)
            contract["decision_ledger"].append({
                "id": decision_id, "decision": decision, "rationale": rationale, "evidence_refs": list(refs),
            })
    return contract, errors


_CONTRACT_FIELDS = (
    "summary",
    "constraints",
    "assumptions",
    "unresolved",
    "verification_preconditions",
    "decision_ledger",
)


def _contract_projection(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Compare only the validated contract fields, never runner metadata."""
    source = raw if isinstance(raw, dict) else {}
    return {field: source.get(field, [] if field != "summary" else "") for field in _CONTRACT_FIELDS}


def _frozen_contract_error(plan: GoalPlan | None, frozen_contract: dict[str, Any] | None) -> str | None:
    if plan is None or frozen_contract is None:
        return None
    if _contract_projection(plan.contract) != _contract_projection(frozen_contract):
        return "execution-time replan attempted to change the frozen goal_contract"
    return None


def _parse_plan_result(
    raw: str, *, test_catalog=None, discovery_manifest: dict[str, Any] | None = None, verification_adapter=None,
    external_dependency_names: tuple[str, ...] = (),
) -> tuple[GoalPlan | None, str | None]:
    """Parse planner output and collect all repairable contract errors."""
    block = _extract_json_object(_strip_agent_header(raw or ""))
    if block is None:
        return None, "response does not contain a complete JSON object"
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None, "response contains malformed JSON"
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        return None, "plan must be an object with a non-empty tasks array"
    if discovery_readiness_error(discovery_manifest):
        return None, discovery_readiness_error(discovery_manifest)
    plans: list[TaskPlan] = []
    errors: list[str] = []
    evidence_ids = {
        str(item.get("id")) for item in (discovery_manifest or {}).get("evidence", []) if isinstance(item, dict)
    }
    contract, contract_errors = _parse_goal_contract(data.get("goal_contract"), evidence_ids)
    errors.extend(contract_errors)
    if "scope_paths" in data:
        errors.append("planning v2 does not support root-level scope_paths")
    names: set[str] = set()
    external_names = set(external_dependency_names)
    generated_paths_by_task: dict[str, frozenset[str]] = {}
    for index, entry in enumerate(data["tasks"], start=1):
        label = f"Task {index}"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be a JSON object")
            continue
        name = str(entry.get("name") or "").strip()[:80]
        behavior = str(entry.get("behavior") or "").strip()[:MAX_BEHAVIOR_CHARS]
        if not name or not behavior or name in names:
            errors.append(f"{label} needs a unique non-empty name and behavior")
            continue
        label = f"Task {index} ({name})"
        cases = _parse_acceptance_cases(entry.get("acceptance_cases"))
        if not cases:
            errors.append(f"{label} needs 1-{MAX_ACCEPTANCE_CASES} valid acceptance_cases")
            continue
        dependencies = _normalise_strings(entry.get("depends_on"))
        if any(dependency not in names and dependency not in external_names for dependency in dependencies):
            errors.append(f"{label} depends_on must list an earlier or completed Task name")
            continue
        dependency_generated_paths = frozenset().union(
            *(generated_paths_by_task.get(dependency, frozenset()) for dependency in dependencies)
        )
        spec = _spec_from_entry(entry, test_catalog, cases, verification_adapter)
        primary_write = _normalise_strings(entry.get("primary_write"))
        planned_new = _normalise_strings(entry.get("planned_new"))
        conditional_write = _normalise_strings(entry.get("conditional_write"))
        # A file declared as planned_new cannot be read yet. Some planners
        # redundantly include it in read_envelope; omit only that impossible
        # overlap, while preserving discovery refreshes for every other
        # unknown path.
        read_envelope = tuple(
            path for path in _normalise_strings(entry.get("read_envelope"))
            if path not in planned_new
        )
        forbidden = _normalise_strings(entry.get("forbidden"))
        requested_refs = _normalise_strings(entry.get("evidence_refs"))
        strategy = str(entry.get("test_strategy") or "")[:1000]
        if "scope_paths" in entry:
            errors.append(f"{label} uses removed scope_paths; use planning v2 scope classes")
        if not primary_write and not planned_new:
            errors.append(f"{label} needs primary_write or planned_new")
        if not strategy:
            errors.append(f"{label} is missing test_strategy")
        if discovery_manifest is not None:
            file_paths = {str(item) for item in discovery_manifest.get("repo_files", [])}
            primary_valid = all(_valid_scope_path(path) and _path_is_existing(path, file_paths) for path in primary_write)
            planned_new_valid = all(_path_can_be_planned_new(path, file_paths) for path in planned_new)
            conditional_valid = all(_valid_scope_path(path) and _path_is_existing(path, file_paths) for path in conditional_write)
            read_valid = all(
                _valid_scope_path(path)
                and (
                    _path_is_existing_envelope(path, file_paths)
                    or path in dependency_generated_paths
                )
                for path in read_envelope
            )
            forbidden_valid = all(_valid_scope_path(path) for path in forbidden)
            evidence_scope = tuple(dict.fromkeys([*primary_write, *conditional_write, *read_envelope]))
            refs = tuple(dict.fromkeys(
                [ref for ref in requested_refs if ref in evidence_ids]
                + list(_inferred_evidence_refs(evidence_scope, discovery_manifest))
            ))
            if primary_write and not primary_valid:
                errors.append(f"{label} primary_write must contain exact discovered files")
            if planned_new and not planned_new_valid:
                errors.append(f"{label} planned_new must be absent and have an evidenced parent directory")
            if conditional_write and not conditional_valid:
                errors.append(f"{label} conditional_write must contain exact discovered files")
            if read_envelope and not read_valid:
                unknown_read_paths = [
                    path for path in read_envelope
                    if not _valid_scope_path(path)
                    or (
                        not _path_is_existing_envelope(path, file_paths)
                        and path not in dependency_generated_paths
                    )
                ]
                errors.append(
                    f"{label} read_envelope must contain discovered files or directories: "
                    + ", ".join(unknown_read_paths)
                )
            if forbidden and not forbidden_valid:
                errors.append(f"{label} forbidden has an invalid workspace path")
            if primary_write and primary_valid and not _task_has_source_evidence(primary_write, refs, discovery_manifest):
                errors.append(f"{label} has no source-code evidence for primary_write")
        else:
            refs = requested_refs
        selected_skills = _normalise_skill_names(entry.get("skills"))
        if not selected_skills:
            selected_skills = _recommended_skill_names(name, behavior, spec)
        plans.append(
            TaskPlan(
                name=name,
                behavior=behavior,
                depends_on=dependencies,
                acceptance_cases=cases,
                skill_names=selected_skills,
                verification_spec=spec,
                primary_write=primary_write,
                planned_new=planned_new,
                conditional_write=conditional_write,
                read_envelope=read_envelope,
                forbidden=forbidden,
                evidence_refs=refs,
                test_strategy=strategy,
                discovery_revision=max(0, int(entry.get("discovery_revision") or 0)),
            )
        )
        names.add(name)
        generated_paths_by_task[name] = dependency_generated_paths | frozenset(planned_new)
    if errors:
        return None, _contract_error_text(errors)
    return GoalPlan(contract=contract or {}, tasks=tuple(plans)), None


def parse_plan(raw: str, *, test_catalog=None, discovery_manifest: dict[str, Any] | None = None, verification_adapter=None) -> GoalPlan | None:
    """Parse a v2 Goal plan without raising."""
    plan, _ = _parse_plan_result(
        raw,
        test_catalog=test_catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
    )
    return plan


def _format_repair_prompt(original_prompt: str, raw: str, error: str) -> str:
    """Ask the planner to repair only its structured response, not rediscover work."""
    previous = _strip_agent_header(raw).strip()[:PLANNER_REPAIR_INPUT_LIMIT]
    return (
        f"{original_prompt}\n\n"
        "Your previous response was rejected before any execution began. "
        f"Contract error: {error}.\n"
        "Return a corrected COMPLETE GoalPlan JSON object now. Preserve valid planning intent, "
        "but fix the contract error. Do not explain the correction, do not call tools, "
        "and do not omit required fields.\n"
        f"Previous response (may be truncated):\n{previous}"
    )


def build_plan_review_prompt(
    plan: GoalPlan,
    *,
    human_language: str = "English",
    completed_task_names: tuple[str, ...] = (),
) -> str:
    """Ask an independent model whether a validated plan is executable."""
    return (
        "You are the independent reviewer for a Goal plan. You do not redesign it and you never grant "
        "permissions. Find only execution-blocking ambiguity, invalid task dependency, scope that is too broad "
        "or too narrow, missing prerequisite, untestable acceptance case, or conflict with the Goal contract.\n\n"
        "A plan is approvable only when a worker can begin each task without deciding product architecture, "
        "and primary_write is narrow exact existing-file scope. conditional_write is not initially writable.\n"
        "Treat persistent implementation artifacts and runtime state separately: a checked-in configuration file may be "
        "primary_write when a Task explicitly changes its schema or shipped defaults. For a current-session or non-persistent "
        "user choice, reject only a plan that writes that runtime choice back to the configuration, or lists configuration "
        "as writable without behavior and acceptance cases that justify the static configuration change.\n"
        "For a plan that says a command changes permission behavior for later tool calls, require explicit scoped coverage of "
        "the command/input registration, current-session state, and centralized enforcement hook. A configuration-only Task "
        "does not satisfy that runtime path. Do not demand a UI/event-stream Task when the Goal is explicitly ordinary-CLI-only.\n"
        f"Retained Task names, when present, are valid dependency anchors: {json.dumps(list(completed_task_names), ensure_ascii=False)}.\n\n"
        f"Plan to review:\n{json.dumps({'goal_contract': plan.contract, 'tasks': [task.to_dict() for task in plan.tasks]}, ensure_ascii=False)}\n\n"
        f"Write all human-readable text in {human_language}. Reply ONLY with JSON:\n"
        '{"approved":true,"summary":"...","findings":[]}\n'
        "or\n"
        '{"approved":false,"summary":"...","findings":[{"severity":"high|medium|low","task":"task name or goal_contract","issue":"specific execution problem","repair":"specific contract correction"}]}\n'
        "No Markdown or prose."
    )


def _review_result(raw: str) -> tuple[bool | None, dict[str, Any] | None, str | None]:
    block = _extract_json_object(_strip_agent_header(raw or ""))
    if block is None:
        return None, None, "reviewer response does not contain a complete JSON object"
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None, None, "reviewer response contains malformed JSON"
    if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
        return None, None, "reviewer response needs boolean approved"
    findings = data.get("findings")
    if not isinstance(findings, list):
        return None, None, "reviewer response needs findings array"
    normalized: list[dict[str, str]] = []
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            return None, None, f"reviewer finding {index} must be an object"
        severity = str(finding.get("severity") or "").lower()
        issue = str(finding.get("issue") or "").strip()
        repair = str(finding.get("repair") or "").strip()
        task = str(finding.get("task") or "goal_contract").strip()
        if severity not in {"high", "medium", "low"} or not issue or not repair:
            return None, None, f"reviewer finding {index} needs severity, issue, and repair"
        normalized.append({"severity": severity, "task": task[:120], "issue": issue[:1000], "repair": repair[:1000]})
    if data["approved"] and normalized:
        return None, None, "reviewer cannot approve while findings are present"
    if not data["approved"] and not normalized:
        return None, None, "reviewer rejection needs at least one finding"
    return bool(data["approved"]), {"approved": bool(data["approved"]), "summary": str(data.get("summary") or "")[:1000], "findings": normalized}, None


def plan_tasks(
    target: str,
    full_verification: str,
    workspace: Path | None = None,
    *,
    planner_runner=None,
    reviewer_runner=None,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
    test_catalog: TestCatalog | None = None,
    discovery_manifest: dict[str, Any] | None = None,
    verification_adapter=None,
    human_language: str = "English",
    frozen_contract: dict[str, Any] | None = None,
    completed_task_names: tuple[str, ...] = (),
    replan_reason: str = "",
    execution_workspace_paths: tuple[str, ...] = (),
    candidate_plan: GoalPlan | None = None,
    candidate_callback: Callable[[GoalPlan], None] | None = None,
) -> GoalPlan:
    """Compile, validate, and independently review a GoalPlan v2."""
    from harness.agents.runner import AgentTaskConversation, AgentTaskStats, run_agent_task as default_runner

    root = (workspace or get_workdir()).resolve()
    if verification_adapter is None:
        verification_adapter = select_adapter(root, full_verification)
    catalog = test_catalog if test_catalog is not None else verification_adapter.discover(VerificationContext(root, command=full_verification))
    runner = planner_runner or default_runner
    reviewer = reviewer_runner or default_runner
    planner_stats = stats if stats is not None else AgentTaskStats()
    planner_conversation = AgentTaskConversation()
    prompt = build_plan_prompt(
        target, full_verification, catalog, discovery_manifest,
        human_language=human_language,
        frozen_contract=frozen_contract,
        completed_task_names=completed_task_names,
        replan_reason=replan_reason,
        execution_workspace_paths=execution_workspace_paths,
    )
    planner_call = {
        "description": "decompose goal into verifiable tasks",
        "prompt": prompt,
        "agent_type": PLANNER_AGENT,
        "cwd": str(root),
        "max_rounds": PLANNER_MAX_ROUNDS,
        "max_tokens": PLANNER_MAX_OUTPUT_TOKENS,
        "tools_override": (),
        # Goal planning returns a potentially large JSON contract. Stream it
        # even when no interactive event sink is active so relay read timeouts
        # measure idle time between chunks rather than whole-response time.
        "stream_response": True,
        "request_read_timeout_seconds": PLANNER_READ_TIMEOUT_SECONDS,
        "max_request_attempts": PLANNER_MAX_REQUEST_ATTEMPTS,
        "cancel_check": cancel_check,
        "deadline": deadline,
        "stats": planner_stats,
        "conversation": planner_conversation,
    }
    raw = ""
    plan = candidate_plan
    contract_error: str | None = None
    if plan is not None:
        raw = json.dumps(
            {"goal_contract": plan.contract, "tasks": [task.to_dict() for task in plan.tasks]},
            ensure_ascii=False,
        )
    else:
        try:
            raw = runner(**planner_call)
        except Exception as exc:
            raise GoalPlanningError(f"Goal planner request failed: {type(exc).__name__}: {exc}") from exc
        if raw.startswith(f"[{PLANNER_AGENT}] failed:") or raw.startswith(f"[{PLANNER_AGENT}] stopped:"):
            raise GoalPlanningError(f"Goal planner is unavailable: {raw}")
        plan, contract_error = _parse_plan_result(
            raw,
            test_catalog=catalog,
            discovery_manifest=discovery_manifest,
            verification_adapter=verification_adapter,
            external_dependency_names=completed_task_names,
        )
        contract_error = contract_error or _frozen_contract_error(plan, frozen_contract)
        if contract_error:
            plan = None
    used_repair = False
    if plan is None:
        if planner_stats.stop_reason == "max_tokens":
            # A complete Task contract can legitimately exceed the normal
            # planning budget. Preserve the partial assistant response and
            # grant one larger continuation instead of restarting discovery.
            planner_stats.stop_reason = None
            continuation_call = dict(planner_call)
            continuation_call.update({
                "description": "complete GoalPlan v2 after output upgrade",
                "prompt": (
                    "Your prior GoalPlan response exhausted its output budget before it formed a complete JSON object. "
                    "Use the retained partial response as context and now return one COMPLETE replacement GoalPlan JSON "
                    "object. Do not explain, call tools, or repeat analysis."
                ),
                "max_tokens": PLANNER_ESCALATED_OUTPUT_TOKENS,
                "deadline": max(
                    deadline or 0.0,
                    time.monotonic() + PLANNER_CONTINUATION_TIMEOUT_SECONDS,
                ),
            })
            try:
                raw = runner(**continuation_call)
            except Exception as exc:
                raise GoalPlanningError(f"Goal planner output continuation failed: {type(exc).__name__}: {exc}") from exc
            if raw.startswith(f"[{PLANNER_AGENT}] failed:") or raw.startswith(f"[{PLANNER_AGENT}] stopped:"):
                raise GoalPlanningError(f"Goal planner output continuation is unavailable: {raw}")
            plan, contract_error = _parse_plan_result(
                raw,
                test_catalog=catalog,
                discovery_manifest=discovery_manifest,
                verification_adapter=verification_adapter,
                external_dependency_names=completed_task_names,
            )
            contract_error = contract_error or _frozen_contract_error(plan, frozen_contract)
            if contract_error:
                plan = None
            if plan is None and planner_stats.stop_reason == "max_tokens":
                raise GoalPlanningError(
                    f"Goal planner exhausted its automatic {PLANNER_ESCALATED_OUTPUT_TOKENS}-token output upgrade "
                    "before returning a complete GoalPlan contract; no execution was started."
                )
        if plan is None:
            repair_call = dict(planner_call)
            repair_call["description"] = "repair GoalPlan v2 JSON"
            repair_call["prompt"] = _format_repair_prompt(prompt, raw, contract_error or "unknown contract error")
            repair_call["max_rounds"] = PLANNER_FORMAT_RETRY_MAX_ROUNDS
            try:
                raw = runner(**repair_call)
            except Exception as exc:
                raise GoalPlanningError(f"Goal planner contract repair failed: {type(exc).__name__}: {exc}") from exc
            used_repair = True
            if raw.startswith(f"[{PLANNER_AGENT}] failed:") or raw.startswith(f"[{PLANNER_AGENT}] stopped:"):
                raise GoalPlanningError(f"Goal planner contract repair is unavailable: {raw}")
            plan, repair_error = _parse_plan_result(
                raw, test_catalog=catalog, discovery_manifest=discovery_manifest,
                verification_adapter=verification_adapter, external_dependency_names=completed_task_names,
            )
            repair_error = repair_error or _frozen_contract_error(plan, frozen_contract)
            if repair_error:
                plan = None
            if plan is None:
                detail = raw.strip().replace("\n", " ")[:500] or "empty response"
                raise GoalPlanningError(
                    "Goal planner returned no valid GoalPlan after one repair attempt; "
                    f"first error: {contract_error or 'unknown'}; repair error: {repair_error or 'unknown'}. Response: {detail}",
                    requires_discovery_refresh=planning_error_requires_discovery(repair_error),
                )

    def review_plan(current: GoalPlan) -> tuple[bool, dict[str, Any], str | None]:
        review_call = {
            "description": "review GoalPlan v2 for execution readiness",
            "prompt": build_plan_review_prompt(
                current,
                human_language=human_language,
                completed_task_names=completed_task_names,
            ),
            "agent_type": PLAN_REVIEWER_AGENT,
            "cwd": str(root), "max_rounds": PLAN_REVIEW_MAX_ROUNDS,
            "max_tokens": PLANNER_MAX_OUTPUT_TOKENS, "tools_override": (),
            "cancel_check": cancel_check, "deadline": deadline, "stats": planner_stats,
        }
        try:
            review_raw = reviewer(**review_call)
        except Exception as exc:
            raise GoalPlanningError(f"Goal plan reviewer request failed: {type(exc).__name__}: {exc}") from exc
        if review_raw.startswith(f"[{PLAN_REVIEWER_AGENT}] failed:") or review_raw.startswith(f"[{PLAN_REVIEWER_AGENT}] stopped:"):
            raise GoalPlanningError(f"Goal plan reviewer is unavailable: {review_raw}")
        approved, result, error = _review_result(review_raw)
        if approved is None or result is None:
            raise GoalPlanningError(f"Goal plan reviewer returned invalid JSON: {error}")
        return approved, result, None

    if candidate_callback is not None:
        candidate_callback(GoalPlan(contract=plan.contract, tasks=plan.tasks))
    approved, review, _ = review_plan(plan)
    if approved:
        return GoalPlan(contract=plan.contract, tasks=plan.tasks, review=review)
    if used_repair:
        raise GoalPlanningError("GoalPlan was rejected after its one permitted correction: " + str(review.get("summary") or review.get("findings")))
    repair_call = dict(planner_call)
    repair_call["description"] = "repair GoalPlan v2 after independent review"
    repair_call["prompt"] = _format_repair_prompt(prompt, raw, "Independent review rejected the plan: " + json.dumps(review, ensure_ascii=False))
    repair_call["max_rounds"] = PLANNER_FORMAT_RETRY_MAX_ROUNDS
    try:
        repaired_raw = runner(**repair_call)
    except Exception as exc:
        raise GoalPlanningError(f"Goal planner review repair failed: {type(exc).__name__}: {exc}") from exc
    repaired, repair_error = _parse_plan_result(
        repaired_raw, test_catalog=catalog, discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter, external_dependency_names=completed_task_names,
    )
    repair_error = repair_error or _frozen_contract_error(repaired, frozen_contract)
    if repair_error:
        repaired = None
    if repaired is None:
        raise GoalPlanningError("Goal planner did not repair the reviewed GoalPlan: " + (repair_error or "unknown error"))
    if candidate_callback is not None:
        candidate_callback(GoalPlan(contract=repaired.contract, tasks=repaired.tasks))
    approved, final_review, _ = review_plan(repaired)
    if not approved:
        raise GoalPlanningError("GoalPlan remains rejected after one correction: " + str(final_review.get("summary") or final_review.get("findings")))
    return GoalPlan(contract=repaired.contract, tasks=repaired.tasks, review=final_review)
