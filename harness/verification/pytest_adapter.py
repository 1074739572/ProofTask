"""Pytest adapter backed by the existing deterministic catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from harness.verification.catalog import TestCatalog, build_pytest_command, collect_pytest_catalog
from harness.verification.runner import run_verification
from harness.verification.adapters import VerificationContext


class PytestAdapter:
    id = "pytest"

    def discover(self, context: VerificationContext) -> TestCatalog:
        return collect_pytest_catalog(context.workspace)

    def normalize_selector(self, value: str) -> str:
        return str(value).strip().replace("\\", "/")

    def build_command(self, selectors: Sequence[str]) -> str:
        return build_pytest_command(list(selectors))

    def run(self, command: str, context: VerificationContext, *, timeout_s: float | None = None):
        return run_verification(command, workspace=context.workspace, timeout_s=timeout_s)
