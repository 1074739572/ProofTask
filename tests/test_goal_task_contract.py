"""Contracts for the latest Goal -> Task -> evidence model."""

from __future__ import annotations

from harness.goal.planner import TaskPlan, parse_plan
from harness.verification.catalog import TestCatalog


def _catalog() -> TestCatalog:
    return TestCatalog(
        selectors=("tests/test_api.py::test_lists_all_pages",),
        test_files=("tests/test_api.py",),
    )


def test_plan_binds_only_catalog_selectors():
    plans = parse_plan(
        '[{"name":"pages","behavior":"all pages return",'
        '"acceptance_cases":[{"id":"AC1","given":"pages","when":"listed","then":"none skipped"}],'
        '"test_selectors":["tests/test_api.py::test_lists_all_pages"],"depends_on":[]}]',
        test_catalog=_catalog(),
    )
    assert plans is not None
    assert isinstance(plans[0], TaskPlan)
    assert plans[0].verification_spec.source == "discovered"
    assert plans[0].verification_spec.selectors == ("tests/test_api.py::test_lists_all_pages",)


def test_unknown_selector_requires_test_generation():
    plans = parse_plan(
        '[{"name":"new","behavior":"new behavior",'
        '"acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"new result"}],'
        '"test_selectors":["tests/invented.py::test_new"],"depends_on":[]}]',
        test_catalog=_catalog(),
    )
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"
    assert not plans[0].verification_spec.command


def test_legacy_verification_command_is_not_a_task_binding():
    plans = parse_plan('[{"name":"x","behavior":"b","acceptance_cases":[{"id":"AC1","given":"input","when":"called","then":"result"}],"verification":"pytest -q","depends_on":[]}]')
    assert plans is not None
    assert plans[0].verification_spec.source == "needs_generation"
