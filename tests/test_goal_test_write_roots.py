"""Regression tests for test-generation write roots.

A Maven/Java Goal must let its test writer create files under
``<module>/src/test/java``.  ``_test_write_roots`` used to return only the
pytest/JS roots (``tests``/``test``/``__tests__``), so a Maven test writer's
write to ``<module>/src/test/java`` was flagged as outside the Task write
scope and the Goal paused on ``permission_wait``.
"""
from types import SimpleNamespace

from harness.goal.runner import GoalRunner
from harness.verification.maven_adapter import MavenTestAdapter
from harness.verification.pytest_adapter import PytestAdapter


def _catalog(test_files=()):
    return SimpleNamespace(test_files=test_files)


def _task(scope=("batch-summary-domain",), planned=("batch-summary-domain",)):
    return SimpleNamespace(scope_paths=scope, planned_new=planned)


def test_maven_write_roots_include_module_src_test_java(tmp_path):
    roots = GoalRunner._test_write_roots(
        _catalog(), adapter=MavenTestAdapter(tmp_path), task=_task(),
    )
    assert "batch-summary-domain/src/test/java" in roots
    assert "src/test/java" in roots


def test_pytest_write_roots_unchanged(tmp_path):
    roots = GoalRunner._test_write_roots(_catalog(), adapter=PytestAdapter())
    assert "tests" in roots
    assert "src/test/java" not in roots


def test_maven_write_roots_skip_dot_and_blank_scope(tmp_path):
    task = SimpleNamespace(scope_paths=[".", "batch-summary-domain"], planned_new=[""])
    roots = GoalRunner._test_write_roots(_catalog(), adapter=MavenTestAdapter(tmp_path), task=task)
    assert "batch-summary-domain/src/test/java" in roots
    assert "/src/test/java" not in roots
