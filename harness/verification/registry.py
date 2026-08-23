"""Project-level adapter selection."""

from __future__ import annotations

import json
from pathlib import Path

from harness.verification.node_adapter import NodeTestAdapter
from harness.verification.pytest_adapter import PytestAdapter


def select_adapter(workspace: str | Path, command: str | None = None):
    root = Path(workspace).expanduser().resolve()
    text = (command or "").lower()
    if "node" in text or "npm test" in text:
        return NodeTestAdapter(root)
    package = root / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and "test" in scripts and "pytest" not in str(scripts["test"]).lower():
            return NodeTestAdapter(root)
    return PytestAdapter()
