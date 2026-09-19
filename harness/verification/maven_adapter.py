"""Maven / Maven Wrapper adapter for JUnit 5 test suites.

Java projects declare tests as JUnit classes under ``src/test/java``.  Maven
has no portable collect-only mode, so the machine -- not the model -- binds the
exact ``-Dtest=`` filter derived from the selectors it discovered.

The selector shape deliberately mirrors the pytest and Node adapters so the
Goal planner can keep deriving the bound test file with
``selector.split("::", 1)[0]``::

    src/test/java/com/example/FooTest.java::com.example.FooTest#rendersSummary

The part before ``::`` is a real repository-relative path, which keeps
``_test_file_hashes`` and the Goal write-root derivation working unchanged,
while the part after ``::`` is the Surefire ``-Dtest=`` node id.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.verification.adapters import VerificationContext
from harness.verification.runner import run_verification


#: JUnit 5 annotations that mark an executable test method.
_TEST_ANNOTATION = r"(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)"

#: An annotation run followed by the method it decorates.
_TEST_METHOD_RE = re.compile(
    r"@(?:" + _TEST_ANNOTATION + r")\b"
    r"(?:\s*\([^()]*(?:\([^()]*\)[^()]*)*\))?"
    r"(?:\s*@[A-Za-z_][\w.]*(?:\s*\([^()]*(?:\([^()]*\)[^()]*)*\))?)*"
    r"\s*(?:(?:public|protected|private)\s+)?(?:static\s+)?(?:final\s+)?"
    r"[A-Za-z_][\w.$]*(?:\s*<[^<>;{}]*>)?(?:\s*\[\s*\])*\s+([A-Za-z_]\w*)\s*\(",
)

_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)

#: Conventional surefire test class names, used when no annotation was parsed.
_TEST_CLASS_NAME_RE = re.compile(r"(?:Test|Tests|TestCase)$")

#: Directories never scanned for test sources.
_SKIPPED_DIRS = frozenset({"target", "node_modules", ".git", ".project", "build", "out"})

#: Deepest module nesting inspected when locating ``src/test/java``.
_MAX_MODULE_DEPTH = 4

#: Surefire's ``-Dtest=`` filter is passed on one command line; degrade to
#: class-level filters before the policy length limit rejects the command.
_MAX_TEST_FILTER_LEN = 900

#: Surefire/Failsafe flags that keep a focused run from failing on modules and
#: classes that simply did not match the requested filter.
_FOCUSED_FLAGS = ("-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false")

#: Fallback catalog command when no wrapper was found.
_DEFAULT_COMMAND = "mvnw.cmd -q test"


@dataclass(frozen=True)
class MavenTestCatalog:
    selectors: tuple[str, ...]
    test_files: tuple[str, ...]
    command: str = _DEFAULT_COMMAND
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.selectors) and self.error is None

    @property
    def adapter(self) -> str:
        return "maven"

    @property
    def collected_count(self) -> int:
        return len(self.selectors)

    def contains(self, selector: str) -> bool:
        return selector.replace("\\", "/") in self.selectors

    def prompt_text(self, *, limit: int = 400) -> str:
        if self.error:
            return f"Maven test catalog unavailable: {self.error}"
        if not self.selectors:
            return "Maven test catalog is empty. Do not invent a selector."
        shown = self.selectors[:max(1, limit)]
        return "\n".join([
            f"Maven test catalog: {len(self.selectors)} discovered selector(s).",
            "Use only an exact selector from this list for test_selectors:",
            *[f"- {item}" for item in shown],
        ])


def _node_id(selector: str) -> str:
    """Surefire node id for one selector: the part after ``::`` when present."""
    text = str(selector or "").strip().replace("\\", "/")
    return text.split("::", 1)[1] if "::" in text else text


def _maven_launcher(root: Path) -> str:
    """Prefer the repository's own wrapper so no system Maven is required."""
    candidates = ("mvnw.cmd", "mvnw.bat") if os.name == "nt" else ("mvnw",)
    for name in candidates:
        if (root / name).is_file():
            return f"./{name}"
    for name in ("mvnw", "mvnw.cmd"):
        if (root / name).is_file():
            return f"./{name}"
    return "mvn"


def build_maven_command(selectors: Sequence[str], *, launcher: str = "mvnw.cmd") -> str:
    """Build a focused ``mvn ... test`` command from discovered selectors.

    Multiple classes are comma-joined into a single ``-Dtest=`` filter.  When
    that filter would exceed the policy length limit, method-level node ids are
    degraded to their owning class so the command stays executable.
    """
    nodes = [node for node in dict.fromkeys(_node_id(item) for item in selectors if str(item or "").strip())]
    if not nodes:
        return f"{launcher} -q test"
    flags = " ".join(_FOCUSED_FLAGS)
    command = f"{launcher} -q {flags} -Dtest={','.join(nodes)} test"
    if len(command) > _MAX_TEST_FILTER_LEN:
        classes = [item for item in dict.fromkeys(node.split("#", 1)[0] for node in nodes) if item]
        if classes:
            command = f"{launcher} -q {flags} -Dtest={','.join(classes)} test"
    return command


class MavenTestAdapter:
    id = "maven"

    def __init__(self, workspace: str | Path | None = None):
        self._workspace = Path(workspace).expanduser().resolve() if workspace is not None else None

    def _launcher(self, root: Path) -> str:
        return _maven_launcher(root)

    def _test_roots(self, root: Path, extra: Sequence[str]) -> tuple[Path, ...]:
        """Locate every ``src/test/java`` this Maven reactor owns."""
        if extra:
            return tuple((root / item).resolve() for item in extra)
        roots: set[Path] = set()
        direct = root / "src" / "test" / "java"
        if direct.is_dir():
            roots.add(direct.resolve())
        # A test-first Goal writes JUnit under ``<module>/src/test/java``
        # *before* the module's ``pom.xml`` exists (the implementation worker
        # creates the pom later).  Discover those directories directly so the
        # catalog still collects the tests instead of silently reporting empty.
        try:
            for candidate in root.rglob("src/test/java"):
                if not candidate.is_dir():
                    continue
                try:
                    parts = candidate.relative_to(root).parts
                except ValueError:
                    continue
                # parts == (<module>..., "src", "test", "java"); module depth is
                # the leading segment count.
                if len(parts) - 3 > _MAX_MODULE_DEPTH:
                    continue
                if any(part in _SKIPPED_DIRS for part in parts):
                    continue
                roots.add(candidate.resolve())
        except OSError:
            pass
        try:
            poms = sorted(root.rglob("pom.xml"))
        except OSError:
            poms = []
        for pom in poms:
            try:
                parts = pom.relative_to(root).parts
            except ValueError:
                continue
            if len(parts) > _MAX_MODULE_DEPTH:
                continue
            if any(part in _SKIPPED_DIRS for part in parts[:-1]):
                continue
            module_test = (pom.parent / "src" / "test" / "java").resolve()
            if module_test.is_dir():
                roots.add(module_test)
        return tuple(sorted(roots))

    def _java_sources(self, root: Path, extra: Sequence[str]) -> list[Path]:
        files: set[Path] = set()
        for base in self._test_roots(root, extra):
            if not base.is_relative_to(root) or not base.is_dir():
                continue
            for path in base.rglob("*.java"):
                if path.is_file():
                    files.add(path)
        return sorted(files)

    @staticmethod
    def _fully_qualified_name(path: Path, text: str) -> str:
        match = _PACKAGE_RE.search(text)
        package = match.group(1) if match else ""
        return f"{package}.{path.stem}" if package else path.stem

    def discover(self, context: VerificationContext) -> MavenTestCatalog:
        root = context.workspace.resolve()
        launcher = self._launcher(root)
        selectors: list[str] = []
        for path in self._java_sources(root, context.test_roots):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fqcn = self._fully_qualified_name(path, text)
            methods = list(dict.fromkeys(
                match.group(1) for match in _TEST_METHOD_RE.finditer(text)
            ))
            if not methods and not _TEST_CLASS_NAME_RE.search(path.stem):
                # Not a test class: no JUnit annotation and no naming signal.
                continue
            rel = path.relative_to(root).as_posix()
            nodes = [f"{fqcn}#{name}" for name in methods] or [fqcn]
            selectors.extend(f"{rel}::{node}" for node in nodes)
        return MavenTestCatalog(
            tuple(selectors),
            tuple(dict.fromkeys(item.split("::", 1)[0] for item in selectors)),
            command=f"{launcher} -q test",
        )

    def normalize_selector(self, value: str) -> str:
        return str(value).strip().replace("\\", "/")

    def build_command(self, selectors: Sequence[str]) -> str:
        launcher = _maven_launcher(self._workspace) if self._workspace is not None else "mvnw.cmd"
        return build_maven_command(selectors, launcher=launcher)

    def build_bootstrap_command(self, pom_path: str = "pom.xml") -> str:
        """Build the test-free gate used only by an explicit greenfield scaffold Task."""
        launcher = _maven_launcher(self._workspace) if self._workspace is not None else "mvn"
        pom = str(pom_path or "pom.xml").strip().replace("\\", "/")
        return f"{launcher} -q -f {pom} -DskipTests validate"

    def run(self, command: str, context: VerificationContext, *, timeout_s: float | None = None):
        return run_verification(command, workspace=context.workspace, timeout_s=timeout_s)
