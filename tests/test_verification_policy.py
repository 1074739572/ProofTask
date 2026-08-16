"""Verification policy regressions for Goal's inferred pytest command."""

from harness.verification.policy import check_verification_command
from harness.verification.runner import _validate_script_path
from harness.verification.catalog import build_pytest_command


def test_goal_default_python_module_pytest_is_accepted(tmp_path):
    command = "python -m pytest -q"

    assert check_verification_command(command).allowed
    assert _validate_script_path(command.split(), tmp_path) is None


def test_python_module_execution_stays_limited_to_pytest():
    assert not check_verification_command("python -m http.server").allowed
    assert not check_verification_command("python -c 'print(1)'").allowed


def test_task_pytest_command_uses_the_same_module_form_as_goal_default():
    command = build_pytest_command(["tests/test_goal_module.py::test_goal_state_has_no_default_lifetime_budget"])

    assert command.startswith("python -m pytest -q ")
    assert check_verification_command(command).allowed


def test_long_parameterized_selector_commands_fall_back_to_the_test_file():
    selectors = [f"tests/test_generated.py::test_case[scenario-{index:03d}]" for index in range(80)]
    command = build_pytest_command(selectors)

    assert command == "python -m pytest -q tests/test_generated.py"
    assert check_verification_command(command).allowed
