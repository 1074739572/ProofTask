from harness.verification.adapters import VerificationContext
from harness.verification.node_adapter import NodeTestAdapter
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
