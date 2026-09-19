"""Regression tests for verification adapter selection.

A JVM Goal must select the Maven adapter even before its ``pom.xml`` exists —
the first Task creates the build skeleton.  Passing a bare adapter id
(``"maven"``) used to fall through to the pytest default because the id is not
a launcher token, which made the test writer generate pytest tests for a
Maven/Java project.
"""
from harness.verification.registry import select_adapter


def test_adapter_id_maven_selects_maven_before_pom_exists(tmp_path):
    assert select_adapter(tmp_path, "maven").id == "maven"


def test_maven_command_selects_maven_before_pom_exists(tmp_path):
    assert select_adapter(tmp_path, "mvn -q test").id == "maven"


def test_adapter_id_pytest_selects_pytest(tmp_path):
    assert select_adapter(tmp_path, "pytest").id == "pytest"


def test_adapter_id_node_selects_node(tmp_path):
    assert select_adapter(tmp_path, "node").id == "node"


def test_empty_command_falls_back_to_pytest(tmp_path):
    assert select_adapter(tmp_path).id == "pytest"
    assert select_adapter(tmp_path, "").id == "pytest"


def test_nested_maven_fixture_does_not_override_pytest_workspace(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    fixture = tmp_path / "fixtures" / "java-sample"
    fixture.mkdir(parents=True)
    (fixture / "pom.xml").write_text("<project />\n", encoding="utf-8")

    assert select_adapter(tmp_path, "pytest -q").id == "pytest"
    assert select_adapter(tmp_path).id == "pytest"
