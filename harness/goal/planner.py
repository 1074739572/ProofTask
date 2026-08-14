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
PLANNER_MAX_ROUNDS = 30
MIN_TASKS = 1
MAX_TASKS = 8
MAX_BEHAVIOR_CHARS = 600
MAX_ACCEPTANCE_CASES = 8
MAX_SELECTORS_PER_TASK = 16

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_AGENT_HEADER = re.compile(r"^\[[^\]]+\] [^\n]*\n*")


class GoalPlanningError(ValueError):
    """Raised when a Goal cannot be decomposed into valid Task contracts."""


def _strip_agent_header(text: str) -> str:
    stripped = text.lstrip()
    if _AGENT_HEADER.match(stripped):
        stripped = _AGENT_HEADER.sub("", stripped, count=1)
    return stripped.strip()


def _normalise_strings(raw: Any, *, limit: int) -> tuple[str, ...]:
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
        if len(values) >= limit:
            break
    return tuple(values)


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
            test_files=_normalise_strings(data.get("test_files"), limit=MAX_SELECTORS_PER_TASK),
            selectors=_normalise_strings(data.get("selectors"), limit=MAX_SELECTORS_PER_TASK),
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
            covers=_normalise_strings(data.get("covers"), limit=MAX_ACCEPTANCE_CASES),
            owners=_normalise_strings(data.get("owners"), limit=MAX_TASKS),
        )


@dataclass(frozen=True)
class TaskPlan:
    """A durable planning unit with machine-verifiable acceptance criteria."""

    name: str
    behavior: str
    depends_on: tuple[str, ...] = ()
    acceptance_cases: tuple[AcceptanceCase, ...] = ()
    verification_spec: VerificationSpec = field(default_factory=VerificationSpec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "behavior": self.behavior,
            "depends_on": list(self.depends_on),
            "acceptance_cases": [case.to_dict() for case in self.acceptance_cases],
            "verification_spec": self.verification_spec.to_dict(),
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
            verification_spec=VerificationSpec.from_dict(spec_raw),
        )


def _parse_acceptance_cases(raw: Any) -> tuple[AcceptanceCase, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        return ()
    cases: list[AcceptanceCase] = []
    ids: set[str] = set()
    for index, item in enumerate(raw[:MAX_ACCEPTANCE_CASES], start=1):
        if not isinstance(item, dict):
            return ()
        case = AcceptanceCase.from_dict(item, index=index)
        if case is None or case.id in ids:
            return ()
        ids.add(case.id)
        cases.append(case)
    return tuple(cases)


def build_plan_prompt(
    target: str,
    full_verification: str,
    test_catalog: TestCatalog | None = None,
) -> str:
    """Prompt for the read-only planner. Output is a JSON Task array only."""
    catalog_text = (test_catalog or TestCatalog(error="not collected")).prompt_text()
    return (
        "You plan one Goal into independently verifiable Tasks. You are read-only.\n\n"
        f"Goal target: {target}\n"
        f"Goal-level full verification command: {full_verification}\n\n"
        "Test binding rules:\n"
        "- The catalog below was collected by the system. A selector is valid only if it "
        "appears there exactly.\n"
        "- Never emit a verification command. The system builds it from test_selectors.\n"
        "- If no catalog selector proves an acceptance case, use an empty test_selectors "
        "array. That explicitly requests a later test-generation phase.\n"
        "- Do not invent files, paths, commands, or selectors.\n\n"
        f"{catalog_text}\n\n"
        f"Split the Goal into {MIN_TASKS}-{MAX_TASKS} Tasks:\n"
        "- Each Task is independently implementable and machine-verifiable.\n"
        "- Each Task must include 1-8 concrete acceptance_cases using given/when/then.\n"
        "- depends_on lists names of earlier Tasks only.\n"
        "- Preserve separately named deliverables as separate Tasks.\n"
        "- Use one Task only when the Goal cannot be meaningfully split.\n\n"
        "Reply with ONLY a JSON array in this schema:\n"
        '[{"name":"paginate list","behavior":"list returns every page",'
        '"acceptance_cases":[{"id":"AC1","given":"more than one page",'
        '"when":"the caller requests pages","then":"no row is skipped"}],'
        '"test_selectors":["tests/test_pagination.py::test_all_pages"],'
        '"depends_on":[]}]\n'
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
    entry: dict[str, Any], catalog: TestCatalog | None, cases: tuple[AcceptanceCase, ...]
) -> VerificationSpec:
    spec_data = entry.get("verification_spec") if isinstance(entry.get("verification_spec"), dict) else {}
    requested = _normalise_strings(
        entry.get("test_selectors", spec_data.get("selectors")),
        limit=MAX_SELECTORS_PER_TASK,
    )

    # Modern planner output is grounded only against an actual Test Catalog.
    if catalog and catalog.available and requested and all(catalog.contains(item) for item in requested):
        files = tuple(dict.fromkeys(item.split("::", 1)[0] for item in requested))
        return VerificationSpec(
            adapter="pytest",
            command=build_pytest_command(requested),
            test_files=files,
            selectors=requested,
            source="discovered",
            collected_count=len(requested),
            baseline_result="not_run",
            confidence="high",
            covers=tuple(case.id for case in cases),
        )
    return VerificationSpec(adapter="pytest", source="needs_generation")


def parse_plan(raw: str, *, test_catalog: TestCatalog | None = None) -> list[TaskPlan] | None:
    """Parse planner output into validated TaskPlans without raising."""
    block = _extract_json_array(_strip_agent_header(raw or ""))
    if block is None:
        return None
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    data = data[:MAX_TASKS]

    plans: list[TaskPlan] = []
    names: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            return None
        name = str(entry.get("name") or "").strip()[:80]
        behavior = str(entry.get("behavior") or "").strip()[:MAX_BEHAVIOR_CHARS]
        if not name or not behavior or name in names:
            return None
        cases = _parse_acceptance_cases(entry.get("acceptance_cases"))
        if not cases:
            return None
        dependencies = _normalise_strings(entry.get("depends_on"), limit=MAX_TASKS)
        if any(dependency not in names for dependency in dependencies):
            return None
        spec = _spec_from_entry(entry, test_catalog, cases)
        plans.append(
            TaskPlan(
                name=name,
                behavior=behavior,
                depends_on=dependencies,
                acceptance_cases=cases,
                verification_spec=spec,
            )
        )
        names.add(name)
    return plans


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
) -> list[TaskPlan]:
    """Decompose a Goal and bind only selectors the system collected."""
    from harness.agents.runner import run_agent_task as default_runner

    root = (workspace or get_workdir()).resolve()
    catalog = test_catalog if test_catalog is not None else collect_pytest_catalog(root)
    runner = planner_runner or default_runner
    try:
        raw = runner(
            description="decompose goal into verifiable tasks",
            prompt=build_plan_prompt(target, full_verification, catalog),
            agent_type=PLANNER_AGENT,
            cwd=str(root),
            max_rounds=PLANNER_MAX_ROUNDS,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
        )
    except Exception:
        raw = ""
    plans = parse_plan(raw, test_catalog=catalog)
    if plans:
        return plans
    raise GoalPlanningError("Goal planner did not produce a valid Task contract; no execution was started")
