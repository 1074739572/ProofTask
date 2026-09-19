from harness.verification.adapters import VerificationContext
from harness.verification.maven_adapter import MavenTestAdapter, MavenTestCatalog, build_maven_command
from harness.verification.node_adapter import NodeTestAdapter
from harness.verification.policy import check_verification_command
from harness.verification.registry import select_adapter
from harness.goal.planner import parse_plan
from harness.tasks import Task
from harness.verification import _migrate_task_runner_to_bun
from harness.verification.runner import _validate_script_path


def test_node_adapter_collects_real_files_and_builds_machine_command(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "app.test.ts").write_text(
        "import { test } from 'node:test';\ntest('adds values', () => {});\n", encoding="utf-8"
    )

    adapter = NodeTestAdapter()
    catalog = adapter.discover(VerificationContext(tmp_path, test_roots=("test",)))

    assert catalog.selectors == ("test/app.test.ts::adds values",)
    assert adapter.build_command(catalog.selectors) == "node --import tsx --test test/app.test.ts"
    assert isinstance(select_adapter(tmp_path, "node --import tsx --test"), NodeTestAdapter)


def test_node_adapter_collects_jsx_test_files(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "app.test.tsx").write_text(
        "import { test } from 'node:test';\ntest('renders JSX', () => {});\n", encoding="utf-8"
    )

    catalog = NodeTestAdapter().discover(VerificationContext(tmp_path, test_roots=("test",)))

    assert catalog.selectors == ("test/app.test.tsx::renders JSX",)
    assert NodeTestAdapter().build_command(catalog.selectors) == "node --import tsx --test test/app.test.tsx"


def test_node_catalog_can_ground_a_planner_binding(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "app.test.ts").write_text("test('adds values', () => {});\n", encoding="utf-8")
    adapter = NodeTestAdapter()
    catalog = adapter.discover(VerificationContext(tmp_path, test_roots=("test",)))
    raw = ('[{"name":"node behavior","behavior":"adds values",'
           '"acceptance_cases":[{"id":"AC1","given":"numbers","when":"added","then":"sum"}],'
           '"test_selectors":["test/app.test.ts::adds values"],'
           '"case_selectors":{"AC1":["test/app.test.ts::adds values"]},"depends_on":[]}]')
    plans = parse_plan(raw, test_catalog=catalog, verification_adapter=adapter)
    assert plans[0].verification_spec.adapter == "node"
    assert plans[0].verification_spec.command == "node --import tsx --test test/app.test.ts"
    assert _validate_script_path(
        ["node", "--import", "tsx", "--test", "test/app.test.ts"], tmp_path
    ) is None


def test_node_adapter_uses_the_vendored_bun_runner_when_available(tmp_path):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    (test_dir / "app.test.ts").write_text("test('adds values', () => {});\n", encoding="utf-8")
    bun = tmp_path / "node_modules" / "@oven" / "bun-windows-x64" / "bin" / "bun.exe"
    bun.parent.mkdir(parents=True)
    bun.write_text("placeholder", encoding="utf-8")

    adapter = NodeTestAdapter(tmp_path)
    catalog = adapter.discover(VerificationContext(tmp_path, test_roots=("test",)))

    assert catalog.command == "./node_modules/@oven/bun-windows-x64/bin/bun.exe test"
    assert adapter.build_command(catalog.selectors) == (
        "./node_modules/@oven/bun-windows-x64/bin/bun.exe test test/app.test.ts"
    )


def test_existing_node_task_binding_migrates_to_the_vendored_bun_runner(tmp_path):
    bun = tmp_path / "node_modules" / "@oven" / "bun-windows-x64" / "bin" / "bun.exe"
    bun.parent.mkdir(parents=True)
    bun.write_text("placeholder", encoding="utf-8")
    task = Task(
        id="task", subject="x", description="x", status="in_progress", owner="goal:x", blockedBy=[],
        verification_spec={
            "adapter": "node",
            "command": "node --import tsx --test test/composer-keybindings.test.ts",
            "selectors": ["test/composer-keybindings.test.ts::works"],
        },
    )

    assert _migrate_task_runner_to_bun(task, tmp_path) is True
    assert task.verification == (
        "./node_modules/@oven/bun-windows-x64/bin/bun.exe test test/composer-keybindings.test.ts"
    )


def _write_java_project(tmp_path, *, module: str | None = "batch-summary-common"):
    """A Maven reactor with the wrapper vendored, as the project contract asks."""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "mvnw.cmd").write_text("@echo off", encoding="utf-8")
    package = tmp_path / (module or "") / "src" / "test" / "java" / "com" / "example"
    package.mkdir(parents=True)
    (tmp_path / (module or "") / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    if module:
        (tmp_path / module / "pom.xml").write_text("<project/>", encoding="utf-8")
    return package


def test_maven_adapter_discovers_junit_selectors_and_builds_surefire_filter(tmp_path):
    package = _write_java_project(tmp_path)
    (package / "SummaryServiceTest.java").write_text(
        "package com.example;\n\n"
        "import org.junit.jupiter.api.Test;\n"
        "class SummaryServiceTest {\n"
        "    @Test\n    void rendersSummary() {}\n\n"
        "    @Test\n    public void rejectsBlankTitle() throws Exception {}\n}\n",
        encoding="utf-8",
    )

    adapter = MavenTestAdapter(tmp_path)
    catalog = adapter.discover(VerificationContext(tmp_path, command="./mvnw.cmd -q test"))

    assert isinstance(catalog, MavenTestCatalog)
    assert catalog.adapter == "maven"
    assert catalog.available is True
    assert catalog.command == "./mvnw.cmd -q test"
    assert catalog.selectors == (
        "batch-summary-common/src/test/java/com/example/SummaryServiceTest.java"
        "::com.example.SummaryServiceTest#rendersSummary",
        "batch-summary-common/src/test/java/com/example/SummaryServiceTest.java"
        "::com.example.SummaryServiceTest#rejectsBlankTitle",
    )
    # The bound test file must stay a real repository path so the Goal runner
    # can hash it and derive its write roots.
    assert catalog.test_files == (
        "batch-summary-common/src/test/java/com/example/SummaryServiceTest.java",
    )
    assert adapter.build_command(catalog.selectors) == (
        "./mvnw.cmd -q -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false "
        "-Dtest=com.example.SummaryServiceTest#rendersSummary,"
        "com.example.SummaryServiceTest#rejectsBlankTitle test"
    )
    decision = check_verification_command(adapter.build_command(catalog.selectors))
    assert decision.allowed is True, decision.reason


def test_maven_adapter_ignores_helpers_and_reads_parameterized_annotations(tmp_path):
    package = _write_java_project(tmp_path, module=None)
    (package / "TestFixtures.java").write_text(
        "package com.example;\n\nfinal class TestFixtures {}\n", encoding="utf-8"
    )
    (package / "SummaryServiceTest.java").write_text(
        "package com.example;\n\n"
        "class SummaryServiceTest {\n"
        "    @ParameterizedTest\n    @ValueSource(strings = {\"a\"})\n"
        "    void rejectsBlankTitle(String title) {}\n}\n",
        encoding="utf-8",
    )

    catalog = MavenTestAdapter(tmp_path).discover(VerificationContext(tmp_path))

    assert catalog.selectors == (
        "src/test/java/com/example/SummaryServiceTest.java"
        "::com.example.SummaryServiceTest#rejectsBlankTitle",
    )


def test_maven_catalog_can_ground_a_planner_binding(tmp_path):
    package = _write_java_project(tmp_path)
    (package / "SummaryServiceTest.java").write_text(
        "package com.example;\n\nclass SummaryServiceTest {\n    @Test\n    void rendersSummary() {}\n}\n",
        encoding="utf-8",
    )
    adapter = MavenTestAdapter(tmp_path)
    catalog = adapter.discover(VerificationContext(tmp_path, command="./mvnw.cmd -q test"))
    selector = (
        "batch-summary-common/src/test/java/com/example/SummaryServiceTest.java"
        "::com.example.SummaryServiceTest#rendersSummary"
    )
    raw = ('[{"name":"summary rendering","behavior":"renders summary",'
           '"acceptance_cases":[{"id":"AC1","given":"markdown","when":"summarized","then":"text"}],'
           f'"test_selectors":["{selector}"],'
           f'"case_selectors":{{"AC1":["{selector}"]}},"depends_on":[]}}]')
    plans = parse_plan(raw, test_catalog=catalog, verification_adapter=adapter)

    assert plans[0].verification_spec.adapter == "maven"
    assert plans[0].verification_spec.test_files == (
        "batch-summary-common/src/test/java/com/example/SummaryServiceTest.java",
    )
    assert plans[0].verification_spec.command == (
        "./mvnw.cmd -q -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false "
        "-Dtest=com.example.SummaryServiceTest#rendersSummary test"
    )


def test_select_adapter_prefers_maven_for_java_workspaces(tmp_path):
    _write_java_project(tmp_path)

    assert select_adapter(tmp_path, "mvnw.cmd -q test").id == "maven"
    assert select_adapter(tmp_path, "").id == "maven"

    bare = tmp_path / "wrapper-only"
    bare.mkdir()
    (bare / "mvnw.cmd").write_text("@echo off", encoding="utf-8")
    assert select_adapter(bare, "mvnw.cmd -q test").id == "maven"


def test_maven_command_degrades_to_classes_before_the_policy_length_limit():
    wide = tuple(
        f"src/test/java/com/example/Class{index:03d}.java"
        f"::com.example.Class{index:03d}#methodName{index:03d}"
        for index in range(40)
    )
    command = build_maven_command(wide, launcher="./mvnw.cmd")

    assert "#" not in command
    assert check_verification_command(command).allowed is True
    assert build_maven_command((), launcher="mvnw.cmd") == "mvnw.cmd -q test"
