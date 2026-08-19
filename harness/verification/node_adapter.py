"""Node's built-in test runner adapter.

Node does not provide a portable collect-only mode.  We therefore bind real
test files (and optional named test declarations) found by the machine; the
adapter, not the model, constructs the executable command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.verification.adapters import VerificationContext
from harness.verification.runner import run_verification


_TEST_CALL_RE = re.compile(r"\b(?:test|it|describe)\s*\(\s*(['\"])(.*?)\1")


@dataclass(frozen=True)
class NodeTestCatalog:
    selectors: tuple[str, ...]
    test_files: tuple[str, ...]
    command: str = "node --import tsx --test"
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.selectors) and self.error is None

    @property
    def adapter(self) -> str:
        return "node"

    @property
    def collected_count(self) -> int:
        return len(self.selectors)

    def contains(self, selector: str) -> bool:
        return selector.replace("\\", "/") in self.selectors

    def prompt_text(self, *, limit: int = 400) -> str:
        if self.error:
            return f"Node test catalog unavailable: {self.error}"
        if not self.selectors:
            return "Node test catalog is empty. Do not invent a selector."
        shown = self.selectors[:max(1, limit)]
        return "\n".join([
            f"Node test catalog: {len(self.selectors)} discovered selector(s).",
            "Use only an exact selector from this list for test_selectors:",
            *[f"- {item}" for item in shown],
        ])


class NodeTestAdapter:
    id = "node"

    def discover(self, context: VerificationContext) -> NodeTestCatalog:
        root = context.workspace.resolve()
        patterns = context.test_roots or ("test", "tests")
        files: list[Path] = []
        for folder in patterns:
            base = (root / folder).resolve()
            if not base.is_relative_to(root) or not base.exists():
                continue
            files.extend(path for path in base.rglob("*.test.ts") if path.is_file())
            files.extend(path for path in base.rglob("*.test.js") if path.is_file())
        selectors: list[str] = []
        for path in sorted(set(files)):
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            names = [match.group(2).strip() for match in _TEST_CALL_RE.finditer(text)]
            selectors.extend(f"{rel}::{name}" for name in dict.fromkeys(names))
            if not names:
                selectors.append(rel)
        return NodeTestCatalog(tuple(selectors), tuple(dict.fromkeys(item.split("::", 1)[0] for item in selectors)))

    def normalize_selector(self, value: str) -> str:
        return str(value).strip().replace("\\", "/")

    def build_command(self, selectors: Sequence[str]) -> str:
        files = list(dict.fromkeys(self.normalize_selector(item).split("::", 1)[0] for item in selectors if item))
        return "node --import tsx --test " + " ".join(files)

    def run(self, command: str, context: VerificationContext, *, timeout_s: float | None = None):
        return run_verification(command, workspace=context.workspace, timeout_s=timeout_s)
