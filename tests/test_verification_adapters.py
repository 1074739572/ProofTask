from harness.verification.adapters import VerificationContext
from harness.verification.node_adapter import NodeTestAdapter
from harness.verification.registry import select_adapter
from harness.goal.planner import parse_plan
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
