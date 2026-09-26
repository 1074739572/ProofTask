import json

from harness.goal.planner import _parse_plan_result
from harness.goal.runner import GoalRunner
from harness.tasks import Task
from harness.verification.maven_adapter import MavenTestAdapter, MavenTestCatalog


def _manifest():
    return {
        "repo_files": ["requirements.md"],
        "repo_dirs": [],
        "evidence": [{"id": "E1", "path": "requirements.md", "claim": "requested behavior"}],
        "summary": "greenfield Maven workspace",
    }


def _contract(tasks):
    return json.dumps({
        "goal_contract": {
            "summary": "create a small Java service",
            "constraints": [],
            "assumptions": [],
            "unresolved": [],
            "verification_preconditions": [],
            "decision_ledger": [{
                "id": "D1",
                "decision": "use Maven",
                "rationale": "the requested project is Java",
                "evidence_refs": ["E1"],
            }],
        },
        "tasks": tasks,
    })


def _case(case_id="AC1"):
    return {"id": case_id, "given": "a clean workspace", "when": "the task runs", "then": "the contract holds"}


def _scaffold():
    return {
        "name": "bootstrap Maven build",
        "behavior": "create a runnable Maven reactor",
        "acceptance_cases": [_case()],
        "depends_on": [],
        "primary_write": [],
        "planned_new": ["pom.xml", "app"],
        "planned_api": [],
        "conditional_write": [],
        "read_envelope": ["requirements.md"],
        "forbidden": [],
        "evidence_refs": ["E1"],
        "test_strategy": "Maven validate proves the scaffold",
        "test_selectors": [],
        "case_selectors": {},
    }


def test_greenfield_maven_plan_gets_explicit_bootstrap_gate_and_planned_api():
    behavior = {
        "name": "implement status mapping",
        "behavior": "map persisted status values",
        "acceptance_cases": [_case("AC2")],
        "depends_on": ["bootstrap Maven build"],
        "primary_write": [],
        "planned_new": ["app/src/main/java/com/example/BatchStatus.java"],
        "planned_api": [{
            "path": "app/src/main/java/com/example/BatchStatus.java",
            "package": "com.example",
            "symbol": "BatchStatus",
            "signatures": ["static BatchStatus fromDatabaseValue(String value)"],
        }],
        "conditional_write": [],
        "read_envelope": ["requirements.md"],
        "forbidden": [],
        "evidence_refs": ["E1"],
        "test_strategy": "JUnit calls the approved status mapping seam",
        "test_selectors": [],
        "case_selectors": {},
    }
    adapter = MavenTestAdapter()
    plan, error = _parse_plan_result(
        _contract([_scaffold(), behavior]),
        test_catalog=MavenTestCatalog((), ()),
        discovery_manifest=_manifest(),
        verification_adapter=adapter,
    )

    assert error is None
    assert plan is not None
    assert plan.tasks[0].verification_spec.source == "bootstrap"
    assert plan.tasks[0].verification_spec.selectors == ("build:validate",)
    assert "-DskipTests validate" in plan.tasks[0].verification_spec.command
    assert plan.tasks[1].planned_api[0]["symbol"] == "BatchStatus"


def test_greenfield_maven_plan_rejects_broad_first_business_task():
    broad = _scaffold()
    broad["planned_new"] = ["app"]
    broad["acceptance_cases"] = [_case("AC1"), _case("AC2"), _case("AC3")]

    plan, error = _parse_plan_result(
        _contract([broad]),
        test_catalog=MavenTestCatalog((), ()),
        discovery_manifest=_manifest(),
        verification_adapter=MavenTestAdapter(),
    )

    assert plan is None
    assert "greenfield Maven scaffold" in error
    assert "at most two" in error


def test_greenfield_maven_plan_rejects_scaffold_as_the_complete_goal():
    plan, error = _parse_plan_result(
        _contract([_scaffold()]),
        test_catalog=MavenTestCatalog((), ()),
        discovery_manifest=_manifest(),
        verification_adapter=MavenTestAdapter(),
    )

    assert plan is None
    assert "cannot contain only its scaffold Task" in error


def test_greenfield_new_source_requires_planner_approved_api():
    behavior = {
        **_scaffold(),
        "name": "implement domain behavior",
        "depends_on": ["bootstrap Maven build"],
        "planned_new": ["app/src/main/java/com/example/BatchStatus.java"],
        "planned_api": [],
    }
    plan, error = _parse_plan_result(
        _contract([_scaffold(), behavior]),
        test_catalog=MavenTestCatalog((), ()),
        discovery_manifest=_manifest(),
        verification_adapter=MavenTestAdapter(),
    )

    assert plan is None
    assert "must declare planned_api" in error


def test_maven_red_baseline_rejects_no_pom_and_unapproved_compile_errors():
    task = Task(
        id="task", subject="x", description="x", status="pending", owner=None, blockedBy=[],
        planned_api=[{
            "path": "app/src/main/java/com/example/BatchStatus.java",
            "package": "com.example",
            "symbol": "BatchStatus",
            "signatures": ["static BatchStatus fromDatabaseValue(String value)"],
        }],
    )

    assert GoalRunner._maven_baseline_infrastructure_failure(
        task, "[ERROR] MissingProjectException: There is no POM in this directory"
    ) is True
    assert GoalRunner._maven_baseline_infrastructure_failure(
        task, "[ERROR] COMPILATION ERROR cannot find symbol class UnplannedService"
    ) is True
    assert GoalRunner._maven_baseline_infrastructure_failure(
        task, "[ERROR] COMPILATION FAILURE cannot find symbol class BatchStatus com.example"
    ) is False


def test_maven_test_root_is_derived_from_planned_java_source():
    task = Task(
        id="task", subject="x", description="x", status="pending", owner=None, blockedBy=[],
        scope_paths=["app/src/main/java/com/example/BatchStatus.java"],
        planned_new=["app/src/main/java/com/example/BatchStatus.java"],
    )

    assert GoalRunner._test_write_roots(
        MavenTestCatalog((), ()), adapter=MavenTestAdapter(), task=task,
    ) == ("app/src/test/java", "src/test/java")
