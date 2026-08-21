"""Goal decomposition planner with machine-checkable Task contracts.

The planning model describes Tasks and acceptance cases.  It may select only
pytest node IDs already discovered by the system; it never gets to invent a
test command and call that proof. Task plans are the only Goal execution
projection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.settings import get_workdir
from harness.verification.catalog import TestCatalog, build_pytest_command, collect_pytest_catalog

PLANNER_AGENT = "goal_planner"
# Planning turns a confirmed requirement plus a machine-collected test catalog
# into a contract. Deep repository inspection belongs to the test writer and
# implementation worker; keeping planning tool-free prevents an invisible,
# unbounded explore loop before a draft can be saved.
PLANNER_MAX_ROUNDS = 1
PLANNER_MAX_OUTPUT_TOKENS = 32_000
PLANNER_FORMAT_RETRY_MAX_ROUNDS = 1
PLANNER_REPAIR_INPUT_LIMIT = 24_000
MAX_BEHAVIOR_CHARS = 600
MAX_ACCEPTANCE_CASES = 8
MAX_SKILLS_PER_TASK = 2
PLANNER_CATALOG_LIMIT = 80

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_AGENT_HEADER = re.compile(r"^\[[^\]]+\] [^\n]*\n*")


class GoalPlanningError(ValueError):
    """Raised when a Goal cannot be decomposed into valid Task contracts."""


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
    scope_paths: tuple[str, ...] = ()
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
            "scope_paths": list(self.scope_paths),
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
            scope_paths=_normalise_strings(data.get("scope_paths")),
            evidence_refs=_normalise_strings(data.get("evidence_refs")),
            test_strategy=str(data.get("test_strategy") or "")[:1000],
            discovery_revision=max(0, int(data.get("discovery_revision") or 0)),
        )


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
        "gaps": [str(item)[:500] for item in discovery_manifest.get("gaps", []) if str(item).strip()],
        "conflicts": [str(item)[:500] for item in discovery_manifest.get("conflicts", []) if str(item).strip()],
        "jobs": jobs,
    }


_SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php", ".cs"})
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
    if repo_source_paths and not source_evidence_paths:
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
    return False


def build_plan_prompt(
    target: str,
    full_verification: str,
    test_catalog: TestCatalog | None = None,
    discovery_manifest: dict[str, Any] | None = None,
    *,
    human_language: str = "English",
) -> str:
    """Prompt for the read-only planner. Output is a JSON Task array only."""
    catalog_text = (test_catalog or TestCatalog(error="not collected")).prompt_text(limit=PLANNER_CATALOG_LIMIT)
    manifest_text = "No Discovery Manifest supplied. Use empty evidence_refs and scope_paths."
    if isinstance(discovery_manifest, dict):
        manifest_text = (
            "Discovery evidence view (machine-collected; cite evidence IDs exactly). "
            "The full repository map remains available only for local validation:\n"
            + json.dumps(_planner_manifest_view(discovery_manifest), ensure_ascii=False)
        )
    return (
        "You plan one Goal into independently verifiable Tasks.\n"
        "The confirmed Goal and system test catalog below are your complete planning evidence. "
        "Return the Task contract directly; do not ask questions, inspect files, or call tools.\n\n"
        f"Goal target: {target}\n"
        f"Goal-level full verification command: {full_verification}\n\n"
        "Test binding rules:\n"
        "- The catalog below was collected by the system. A selector is valid only if it "
        "appears there exactly.\n"
        "- Never emit a verification command. The system builds it from test_selectors.\n"
        "- Use existing test_selectors only when every acceptance case has an exact case_selectors mapping. "
        "Otherwise use an empty test_selectors array; the system will generate focused tests later.\n"
        "- Do not invent files, paths, commands, or selectors.\n\n"
        f"{catalog_text}\n\n"
        f"{manifest_text}\n\n"
        "Split the Goal into as many Tasks as its independently verifiable deliverables require:\n"
        "- First compare every requested behavior against Discovery evidence and classify it internally as implemented, partial, missing, or unknown. Create Tasks only for partial or missing behavior; implemented behavior may need regression coverage but is not implementation work.\n"
        "- There is no target Task count. Cover every distinct deliverable; never merge work merely to reduce the count.\n"
        "- Each Task is independently implementable and machine-verifiable.\n"
        "- Each Task must include 1-8 concrete acceptance_cases using given/when/then; split a Task that needs more.\n"
        "- depends_on lists names of earlier Tasks only.\n"
        "- scope_paths must be selected from scope_candidates or evidence.path. The system derives evidence_refs from that scope; include extra evidence IDs only when they add requirement context.\n"
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
        "Reply with ONLY a JSON array in this schema:\n"
        '[{"name":"paginate list","behavior":"list returns every page",'
        '"acceptance_cases":[{"id":"AC1","given":"more than one page",'
        '"when":"the caller requests pages","then":"no row is skipped"}],'
        '"test_selectors":["tests/test_pagination.py::test_all_pages"],'
        '"depends_on":[],"scope_paths":["src/list.py"],"evidence_refs":["E1"],'
        '"test_strategy":"one selector per acceptance case", "case_selectors":{"AC1":["tests/test_pagination.py::test_all_pages"]},'
        '"skills":["test-driven-development"]}]\n'
        "No prose and no code fence."
    )


def _extract_json_array(text: str) -> str | None:
    if not text or not text.strip():
        return None
    for candidate in _FENCE_RE.findall(text):
        candidate = candidate.strip()
        if candidate.startswith("["):
            return candidate
    start = text.find("[")
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
        elif char == "[":
            depth += 1
        elif char == "]":
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


def _parse_plan_result(
    raw: str, *, test_catalog=None, discovery_manifest: dict[str, Any] | None = None, verification_adapter=None,
) -> tuple[list[TaskPlan] | None, str | None]:
    """Parse planner output and collect all repairable contract errors."""
    block = _extract_json_array(_strip_agent_header(raw or ""))
    if block is None:
        return None, "response does not contain a complete JSON array"
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None, "response contains malformed JSON"
    if not isinstance(data, list) or not data:
        return None, "plan must be a non-empty JSON array"
    if discovery_readiness_error(discovery_manifest):
        return None, discovery_readiness_error(discovery_manifest)
    plans: list[TaskPlan] = []
    errors: list[str] = []
    names: set[str] = set()
    for index, entry in enumerate(data, start=1):
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
        if any(dependency not in names for dependency in dependencies):
            errors.append(f"{label} depends_on must list only earlier Task names")
            continue
        spec = _spec_from_entry(entry, test_catalog, cases, verification_adapter)
        scopes = _normalise_strings(entry.get("scope_paths"))
        requested_refs = _normalise_strings(entry.get("evidence_refs"))
        strategy = str(entry.get("test_strategy") or "")[:1000]
        if discovery_manifest is not None:
            evidence_ids = {str(item.get("id")) for item in discovery_manifest.get("evidence", []) if isinstance(item, dict)}
            file_paths = {str(item) for item in discovery_manifest.get("repo_files", [])}
            # A scope may be an existing file or a directory that contains a
            # discovered file. The latter is required for planned new files.
            scopes_valid = all(
                _valid_scope_path(scope)
                and any(path == scope or path.startswith(scope.rstrip("/") + "/") for path in file_paths)
                for scope in scopes
            )
            refs = tuple(dict.fromkeys(
                [ref for ref in requested_refs if ref in evidence_ids]
                + list(_inferred_evidence_refs(scopes, discovery_manifest))
            ))
            if not scopes:
                errors.append(f"{label} is missing scope_paths")
            if not strategy:
                errors.append(f"{label} is missing test_strategy")
            if scopes and not scopes_valid:
                errors.append(f"{label} scope_paths must be discovered workspace files or directories")
            if scopes and scopes_valid and not _task_has_source_evidence(scopes, refs, discovery_manifest):
                errors.append(f"{label} has no source-code evidence for its code scope")
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
                scope_paths=scopes,
                evidence_refs=refs,
                test_strategy=strategy,
                discovery_revision=max(0, int(entry.get("discovery_revision") or 0)),
            )
        )
        names.add(name)
    if errors:
        return None, _contract_error_text(errors)
    return plans, None


def parse_plan(raw: str, *, test_catalog=None, discovery_manifest: dict[str, Any] | None = None, verification_adapter=None) -> list[TaskPlan] | None:
    """Parse planner output into validated TaskPlans without raising."""
    plans, _ = _parse_plan_result(
        raw,
        test_catalog=test_catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
    )
    return plans


def _format_repair_prompt(original_prompt: str, raw: str, error: str) -> str:
    """Ask the planner to repair only its structured response, not rediscover work."""
    previous = _strip_agent_header(raw).strip()[:PLANNER_REPAIR_INPUT_LIMIT]
    return (
        f"{original_prompt}\n\n"
        "Your previous response was rejected before any execution began. "
        f"Contract error: {error}.\n"
        "Return a corrected COMPLETE JSON array now. Preserve valid planning intent, "
        "but fix the contract error. Do not explain the correction, do not call tools, "
        "and do not omit required fields.\n"
        f"Previous response (may be truncated):\n{previous}"
    )


def plan_tasks(
    target: str,
    full_verification: str,
    workspace: Path | None = None,
    *,
    planner_runner=None,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats=None,
    test_catalog: TestCatalog | None = None,
    discovery_manifest: dict[str, Any] | None = None,
    verification_adapter=None,
    human_language: str = "English",
) -> list[TaskPlan]:
    """Decompose a Goal and bind only selectors the system collected."""
    from harness.agents.runner import AgentTaskStats, run_agent_task as default_runner

    root = (workspace or get_workdir()).resolve()
    if verification_adapter is None:
        from harness.verification import VerificationContext, select_adapter

        verification_adapter = select_adapter(root, full_verification)
    catalog = test_catalog if test_catalog is not None else verification_adapter.discover(VerificationContext(root, command=full_verification))
    runner = planner_runner or default_runner
    planner_stats = stats if stats is not None else AgentTaskStats()
    prompt = build_plan_prompt(
        target, full_verification, catalog, discovery_manifest,
        human_language=human_language,
    )
    planner_call = {
        "description": "decompose goal into verifiable tasks",
        "prompt": prompt,
        "agent_type": PLANNER_AGENT,
        "cwd": str(root),
        "max_rounds": PLANNER_MAX_ROUNDS,
        "max_tokens": PLANNER_MAX_OUTPUT_TOKENS,
        "tools_override": (),
        "cancel_check": cancel_check,
        "deadline": deadline,
        "stats": planner_stats,
    }
    try:
        raw = runner(**planner_call)
    except Exception as exc:
        raise GoalPlanningError(f"Goal planner request failed: {type(exc).__name__}: {exc}") from exc
    if raw.startswith(f"[{PLANNER_AGENT}] failed:") or raw.startswith(f"[{PLANNER_AGENT}] stopped:"):
        raise GoalPlanningError(f"Goal planner is unavailable: {raw}")
    plans, contract_error = _parse_plan_result(
        raw,
        test_catalog=catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
    )
    if plans:
        return plans
    if planner_stats.stop_reason == "max_tokens":
        raise GoalPlanningError(
            f"Goal planner exhausted its {PLANNER_MAX_OUTPUT_TOKENS}-token output budget before returning "
            "a complete Task contract; no execution was started."
        )
    repair_call = dict(planner_call)
    repair_call["description"] = "repair Goal Task contract JSON"
    repair_call["prompt"] = _format_repair_prompt(prompt, raw, contract_error or "unknown contract error")
    repair_call["max_rounds"] = PLANNER_FORMAT_RETRY_MAX_ROUNDS
    try:
        repaired_raw = runner(**repair_call)
    except Exception as exc:
        raise GoalPlanningError(f"Goal planner contract repair failed: {type(exc).__name__}: {exc}") from exc
    if repaired_raw.startswith(f"[{PLANNER_AGENT}] failed:") or repaired_raw.startswith(f"[{PLANNER_AGENT}] stopped:"):
        raise GoalPlanningError(f"Goal planner contract repair is unavailable: {repaired_raw}")
    plans, repair_error = _parse_plan_result(
        repaired_raw,
        test_catalog=catalog,
        discovery_manifest=discovery_manifest,
        verification_adapter=verification_adapter,
    )
    if plans:
        return plans
    if planner_stats.stop_reason == "max_tokens":
        raise GoalPlanningError(
            f"Goal planner contract repair exhausted its {PLANNER_MAX_OUTPUT_TOKENS}-token output budget before "
            "returning a complete Task contract; no execution was started."
        )
    detail = repaired_raw.strip().replace("\n", " ")[:500] or "empty response"
    raise GoalPlanningError(
        "Goal planner returned no valid Task contract after one repair attempt; "
        f"first error: {contract_error or 'unknown'}; repair error: {repair_error or 'unknown'}. Response: {detail}"
    )
