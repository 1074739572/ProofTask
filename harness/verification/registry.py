"""Project-level adapter selection."""

from __future__ import annotations

import json
from pathlib import Path

from harness.verification.maven_adapter import MavenTestAdapter
from harness.verification.node_adapter import NodeTestAdapter
from harness.verification.pytest_adapter import PytestAdapter


#: Launcher names that identify a Maven verification command. The Maven
#: Wrapper (``mvnw`` / ``mvnw.cmd``) is the project contract for JDK 17 +
#: Spring Boot builds, so it must select the Maven adapter even when no
#: ``pom.xml`` exists yet (the first task is what creates it).
MAVEN_LAUNCHERS = frozenset({"mvn", "mvnw"})


def _leading_launcher(command: str) -> str:
    """Bare program name of a command string, without path or script suffix."""
    tokens = str(command or "").strip().split()
    if not tokens:
        return ""
    name = tokens[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _has_java_markers(root: Path) -> bool:
    """True when the workspace is a Maven project or ships a Maven Wrapper."""
    if (root / "pom.xml").is_file():
        return True
    if any((root / name).is_file() for name in ("mvnw", "mvnw.cmd", "mvnw.bat")):
        return True
    # Do not scan arbitrary first-level directories here.  A repository can
    # contain a fixture, vendored example, or unrelated nested Maven project;
    # treating that as the active workspace changes a plain `pytest -q` Goal
    # into Maven and makes Draft planning reject otherwise valid plans.  A
    # multi-module Maven reactor is identified by its root pom (or by an
    # explicit mvn/mvnw verification command) above.
    return False


def select_adapter(workspace: str | Path, command: str | None = None):
    root = Path(workspace).expanduser().resolve()
    text = (command or "").lower()
    # An explicit adapter id ("maven"/"pytest"/"node") wins before any command
    # or project sniffing.  Callers hand over a Task's ``verification_spec``
    # adapter id directly; a bare id like "maven" is not a launcher token and
    # must not fall through to the pytest default (which would make a JVM Goal
    # generate pytest tests before its pom.xml exists).
    if text == "maven":
        return MavenTestAdapter(root)
    if text == "pytest":
        return PytestAdapter()
    if text == "node":
        return NodeTestAdapter(root)
    if "node" in text or "npm test" in text:
        return NodeTestAdapter(root)
    # An explicit Maven launcher always wins over generic project sniffing.
    if _leading_launcher(command or "") in MAVEN_LAUNCHERS:
        return MavenTestAdapter(root)
    package = root / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and "test" in scripts and "pytest" not in str(scripts["test"]).lower():
            return NodeTestAdapter(root)
    # A Java workspace with no explicit command hint is still a Maven project.
    if _has_java_markers(root):
        return MavenTestAdapter(root)
    return PytestAdapter()
