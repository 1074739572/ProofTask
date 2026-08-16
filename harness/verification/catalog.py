"""Deterministic pytest test discovery for Goal planning.

The planner must not invent a test path and call it a binding.  This module
collects real pytest node IDs before planning so a task can only reference a
selector the system has observed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_NODE_ID_RE = re.compile(r"^(?P<path>.+?\.py)(?:::.+)+$")
_MAX_PROMPT_SELECTORS = 400


def _normalise_selector(value: str) -> str:
    return value.strip().replace("\\", "/")


def _selector_path(selector: str) -> str:
    return selector.split("::", 1)[0]


@dataclass(frozen=True)
class TestCatalog:
    """The actual pytest tests collectable in one workspace."""

    __test__ = False  # This is a data model, not a pytest test class.

    selectors: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    command: str = "pytest --collect-only -q"
    error: str | None = None
    truncated: bool = False

    @property
    def adapter(self) -> str:
        return "pytest"

    @property
    def collected_count(self) -> int:
        return len(self.selectors)

    @property
    def available(self) -> bool:
        return self.error is None and self.collected_count > 0

    def contains(self, selector: str) -> bool:
        return _normalise_selector(selector) in set(self.selectors)

    def prompt_text(self, *, limit: int = _MAX_PROMPT_SELECTORS) -> str:
        """Render a bounded catalog for the read-only planning model."""
        if self.error:
            return f"Pytest catalog unavailable: {self.error}"
        if not self.selectors:
            return "Pytest catalog is empty. Do not invent a selector."
        shown = self.selectors[: max(1, limit)]
        lines = [
            f"Pytest catalog: {self.collected_count} collected selector(s).",
            "Use only an exact selector from this list for test_selectors:",
            *[f"- {selector}" for selector in shown],
        ]
        if len(shown) < len(self.selectors) or self.truncated:
            lines.append("Catalog output is truncated; use needs_generation if no shown selector fits.")
        return "\n".join(lines)


def parse_pytest_collection_output(output: str, workspace: str | Path) -> tuple[str, ...]:
    """Extract existing Python pytest node IDs from ``--collect-only -q`` output."""
    root = Path(workspace).expanduser().resolve()
    selectors: list[str] = []
    seen: set[str] = set()
    for raw in (output or "").splitlines():
        candidate = _normalise_selector(raw)
        match = _NODE_ID_RE.match(candidate)
        if not match:
            continue
        path = _selector_path(candidate)
        try:
            resolved = (root / path).resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        if candidate not in seen:
            seen.add(candidate)
            selectors.append(candidate)
    return tuple(selectors)


def build_pytest_command(selectors: tuple[str, ...] | list[str]) -> str:
    """Build the only pytest command used for a bound Task verification."""
    # Running pytest as a module preserves the workspace on ``sys.path``.
    # The console-script form can otherwise fail to import the harness package
    # even though the same test succeeds under Goal's inferred global command.
    normalised = [_normalise_selector(item) for item in selectors]
    argv = ["python", "-m", "pytest", "-q", *normalised]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    # Parameterized node ids can exceed the verification policy's command
    # budget. The file remains a collected, grounded target and exercises the
    # same generated coverage without a lossy or arbitrary truncation.
    if len(command) > 900:
        files = list(dict.fromkeys(item.split("::", 1)[0] for item in normalised))
        argv = ["python", "-m", "pytest", "-q", *files]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return command


def collect_pytest_catalog(
    workspace: str | Path,
    *,
    timeout_s: float = 45.0,
    max_selectors: int = 2_000,
) -> TestCatalog:
    """Run a bounded, cache-free pytest collection and return real node IDs.

    ``pytest`` collection necessarily imports test modules, but this invocation
    disables pytest's cache provider and Python bytecode writes.  It never
    executes test functions and never uses a shell.
    """
    root = Path(workspace).expanduser().resolve()
    command = "pytest --collect-only -q -p no:cacheprovider"
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=max(1.0, min(float(timeout_s), 300.0)),
            env=env,
        )
    except FileNotFoundError:
        return TestCatalog(command=command, error="pytest is not installed")
    except subprocess.TimeoutExpired:
        return TestCatalog(command=command, error=f"pytest collection timed out after {timeout_s:g}s")
    except OSError as exc:
        return TestCatalog(command=command, error=f"pytest collection failed: {exc}")

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    selectors = parse_pytest_collection_output(output, root)
    if proc.returncode not in (0, 5):
        detail = output.strip().splitlines()[-1] if output.strip() else f"exit code {proc.returncode}"
        return TestCatalog(command=command, error=f"pytest collection failed: {detail[:300]}")
    if not selectors:
        return TestCatalog(command=command, error="pytest collected zero test selectors")

    trimmed = selectors[:max_selectors]
    files = tuple(dict.fromkeys(_selector_path(selector) for selector in trimmed))
    return TestCatalog(
        selectors=trimmed,
        test_files=files,
        command=command,
        truncated=len(trimmed) < len(selectors),
    )
