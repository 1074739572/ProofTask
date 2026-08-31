"""Evaluation input assembly (L5).

Collects everything an independent evaluator needs to judge a feature:

- the original requirement (``feature.behavior``)
- the declared verification (``feature.verification``)
- the machine verification evidence (L3, ``feature.evidence``)
- the diff of what actually changed (git diff in the feature workspace)
- the scoring rubric (fixed checklist)

The evaluator is read-only and advisory: its findings are recorded on the
feature (``feature.evaluation``) but never change feature state.
"""

from __future__ import annotations

import json
import subprocess
from difflib import unified_diff
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.features import Feature, get_feature

#: Fixed rubric the evaluator judges against.
RUBRIC: list[str] = [
    "需求达成度: 改动是否完整实现了 behavior 描述的原始需求",
    "范围控制: 改动是否局限于必要文件，有无越界/无关修改",
    "证据一致性: 机器验证证据与声明的 verification 是否一致",
    "完成声明: feature 状态与代码实际实现是否匹配",
]

MAX_DIFF_CHARS = 30_000
MAX_BOUND_TEST_CHARS = 12_000

# Never place credential/configuration material into an evaluator prompt. A
# task cannot legitimately require changing these files; permissions already
# deny them, and reading an untracked backup would leak secrets to a model.
_SENSITIVE_UNTRACKED_NAMES = frozenset({"id_rsa", "id_ed25519"})


def _is_sensitive_untracked(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        name in _SENSITIVE_UNTRACKED_NAMES
        or name.startswith(".env")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


@dataclass
class EvaluationInputs:
    feature: Feature
    diff: str
    bound_test_sources: list[tuple[str, str]] = field(default_factory=list)
    rubric: list[str] = field(default_factory=lambda: list(RUBRIC))
    effective_evidence: list[dict[str, Any]] | None = None

    def to_text(self) -> str:
        """Render the inputs as the prompt body for the evaluator agent."""
        lines = [
            "# 原始需求 (behavior)",
            self.feature.behavior or "(空)",
            "",
            "# Task 验收案例 (acceptance cases)",
        ]
        cases = self.feature.acceptance_cases or []
        if cases:
            for case in cases:
                if not isinstance(case, dict):
                    continue
                lines.append(
                    "- {id}: Given {given}; When {when}; Then {then}".format(
                        id=case.get("id", "AC"),
                        given=case.get("given", ""),
                        when=case.get("when", ""),
                        then=case.get("then", ""),
                    )
                )
        else:
            lines.append("(legacy Task: no explicit acceptance cases)")
        approved_writes = [
            *getattr(self.feature, "primary_write", []),
            *getattr(self.feature, "planned_new", []),
            *getattr(self.feature, "conditional_write", []),
        ]
        lines += [
            "",
            "# Approved implementation scope",
            json.dumps(list(dict.fromkeys(str(path) for path in approved_writes if str(path).strip())), ensure_ascii=False),
        ]
        lines += [
            "",
            "# 验证绑定 (VerificationSpec)",
            json.dumps(self.feature.verification_spec or {}, ensure_ascii=False, sort_keys=True),
            "",
            "# 声明的验证命令 (verification)",
            self.feature.verification or "(空)",
            "",
            "# 机器验证证据 (evidence)",
        ]
        evidence = (
            self.effective_evidence
            if self.effective_evidence is not None
            else self.feature.evidence
        )
        spec = self.feature.verification_spec if isinstance(self.feature.verification_spec, dict) else {}
        impact_context = spec.get("impact_context") or []
        if isinstance(impact_context, list):
            impact_context = [entry for entry in impact_context if isinstance(entry, dict)][-8:]
        else:
            impact_context = []
        if impact_context:
            lines += [
                "",
                "# Cross-Task impact context",
                json.dumps(impact_context, ensure_ascii=False, sort_keys=True)[:5000],
            ]
        if evidence:
            if self.effective_evidence is not None and len(self.feature.evidence) > len(evidence):
                lines.append(
                    "Earlier verification runs are retained for audit but are not evidence of the current code state."
                )
            for ev in evidence:
                lines.append(
                    f"- command: {ev.get('command', '')} | "
                    f"exit_code: {ev.get('exit_code')} | "
                    f"verified_by: {ev.get('verified_by', '')} | "
                    f"collected_count: {ev.get('collected_count', 0)}"
                )
                selectors = ev.get("selectors") or []
                if selectors:
                    lines.append(f"  selectors: {', '.join(str(item) for item in selectors[:32])}")
                tail = (ev.get("stdout_tail") or "").strip()
                if tail:
                    lines.append(f"  stdout_tail: {tail[:500]}")
                diagnostics = ev.get("diagnostics") if isinstance(ev.get("diagnostics"), dict) else {}
                if diagnostics:
                    lines.append(
                        "  diagnostics: "
                        f"mode={diagnostics.get('failure_mode') or '(unknown)'}; "
                        f"signature={diagnostics.get('failure_signature') or '(none)'}; "
                        f"blocked_before_assertions={diagnostics.get('blocked_before_assertions')}"
                    )
                    bundle = diagnostics.get("debug_bundle")
                    if isinstance(bundle, dict):
                        lines.append(f"  debug_bundle_json: {bundle.get('json') or '(missing)'}")
                        lines.append(f"  debug_bundle_report: {bundle.get('markdown') or '(missing)'}")
        else:
            lines.append("(无证据 — 该 feature 尚未通过机器验证)")
        lines += ["", "# 绑定测试源码 (bound test sources)"]
        if self.bound_test_sources:
            for path, source in self.bound_test_sources:
                lines.extend([f"--- {path} ---", source])
        else:
            lines.append("(无可读取的绑定测试文件)")
        lines += ["", "# 实际改动 (git diff)", self.diff or "(无 diff 或非 git 仓库)", ""]
        lines += ["# 评分标准", *[f"- {r}" for r in self.rubric]]
        lines += [
            "",
            "# Diff interpretation",
            "This diff contains only currently uncommitted changes attributable to this Task. "
            "A clean diff can mean the Task work was committed after verification; it is not proof that the implementation is absent. "
            "Do not fail scope merely because an expected implementation file is absent from an empty diff. "
            "Judge scope violations only from unrelated files actually present in this Task diff.",
        ]
        start_snapshot = getattr(self.feature, "start_snapshot", None)
        start_dirty_hashes = getattr(self.feature, "start_dirty_hashes", None)
        if start_snapshot or start_dirty_hashes:
            lines.extend(
                [
                    "# Task start baseline",
                    f"snapshot: {start_snapshot or '(not recorded)'}",
                    "Pre-existing dirty files are an attribution baseline, not current Task changes: "
                    + json.dumps(sorted(start_dirty_hashes) if isinstance(start_dirty_hashes, dict) else []),
                    "Their historical diff is intentionally omitted. Judge scope and behavior from the Task-local diff above.",
                ]
            )
        return "\n".join(lines)


def _current_task_evidence(task: Any) -> list[dict[str, Any]]:
    """Select the proof that represents the Task's current verification state.

    A red baseline remains durable audit history, but it must not make a later
    matching green verification permanently fail the evaluator. The Task state
    machine likewise uses the latest verification result, so evaluator input
    follows that same current-state rule.
    """
    evidence = [item for item in (getattr(task, "evidence", None) or []) if isinstance(item, dict)]
    if not evidence:
        return []
    spec = getattr(task, "verification_spec", None)
    command = str(spec.get("command") or "") if isinstance(spec, dict) else ""
    if command:
        matching = [item for item in evidence if str(item.get("command") or "") == command]
        if matching:
            return [matching[-1]]
    return [evidence[-1]]


def _git_diff(workspace: Path, paths: set[str] | None = None) -> str:
    """Diff of tracked changes PLUS the full content of untracked files —
    an evaluator must see new files, not just modifications."""
    parts: list[str] = []
    try:
        command = ["git", "diff", "HEAD"]
        if paths is not None:
            if not paths:
                return ""
            command.extend(["--", *sorted(paths)])
        proc = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode == 0:
        parts.append(proc.stdout)
    else:
        try:
            command = ["git", "diff"]
            if paths is not None:
                command.extend(["--", *sorted(paths)])
            proc = subprocess.run(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode == 0:
            parts.append(proc.stdout)

    # Untracked files: read their content so the evaluator sees new code.
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        status = None
    if status is not None and status.returncode == 0:
        untracked: list[str] = []
        for line in status.stdout.splitlines():
            if line.startswith("?? "):
                rel = line[3:].strip()
                normalized = rel.replace("\\", "/")
                if (
                    rel
                    and not _is_sensitive_untracked(normalized)
                    and (paths is None or normalized in paths)
                ):
                    untracked.append(rel)
        for rel in untracked[:50]:
            path = workspace / rel
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                parts.append(f"\n--- untracked: {rel} ---\n{content[:8000]}")

    text = "\n".join(p for p in parts if p)
    return text[:MAX_DIFF_CHARS]


def _task_scoped_diff(task, workspace: Path) -> str:
    """Render only changes attributable to this Task from its claim baseline."""
    from harness.verification.snapshot import capture_dirty_file_hashes

    baseline = getattr(task, "start_dirty_hashes", None)
    if not isinstance(baseline, dict):
        # Legacy tasks have no usable baseline. Keep their historical behavior.
        return _git_diff(workspace)
    current = capture_dirty_file_hashes(workspace)
    changed = {
        path for path, digest in current.items()
        if baseline.get(path) != digest
    }
    scope_paths = {
        str(path).replace("\\", "/").strip("/")
        for path in (getattr(task, "scope_paths", None) or [])
        if str(path).strip("/")
    }
    if scope_paths:
        changed = {
            path
            for path in changed
            if any(path == scope or path.startswith(f"{scope}/") for scope in scope_paths)
        }
    # A recovery may restore a multi-file Goal patch before a replacement Task
    # is claimed.  For a pre-existing dirty file edited again by this Task,
    # render a content-to-content delta.  Passing it through ``git diff`` would
    # show the whole restored file and falsely attribute earlier Task work.
    prior_contents = getattr(task, "start_dirty_contents", None)
    prior_contents = prior_contents if isinstance(prior_contents, dict) else {}
    current_contents: dict[str, str] = {}
    inherited_changed: set[str] = set()
    for path in changed:
        before = prior_contents.get(path)
        candidate = workspace / path
        if not isinstance(before, str) or not candidate.is_file():
            continue
        try:
            after = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        current_contents[path] = "".join(
            unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path} (Task start)",
                tofile=f"b/{path} (current)",
                n=0,
            )
        )
        inherited_changed.add(path)
    rendered = _git_diff(workspace, changed - inherited_changed)
    task_deltas = "\n".join(delta for delta in current_contents.values() if delta)
    return "\n".join(part for part in (rendered, task_deltas) if part)[:MAX_DIFF_CHARS]


def _bound_test_sources(feature: Feature, workspace: Path) -> list[tuple[str, str]]:
    spec = feature.verification_spec if isinstance(feature.verification_spec, dict) else {}
    sources: list[tuple[str, str]] = []
    remaining = MAX_BOUND_TEST_CHARS
    for raw in spec.get("test_files") or []:
        if remaining <= 0:
            break
        rel = str(raw).replace("\\", "/")
        try:
            path = (workspace / rel).resolve()
            if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")[:remaining]
        except OSError:
            continue
        sources.append((rel, source))
        remaining -= len(source)
    return sources


def collect_inputs(feature_id: str, workspace: str | Path | None = None) -> EvaluationInputs:
    """Assemble the inputs for evaluating one feature."""
    feature: Feature = get_feature(feature_id, workspace)
    ws = Path(feature.workspace or workspace or ".")
    return EvaluationInputs(
        feature=feature,
        diff=_git_diff(ws),
        bound_test_sources=_bound_test_sources(feature, ws),
    )


def collect_task_inputs(task_id: str, workspace: str | Path) -> EvaluationInputs:
    """Assemble evaluator inputs from a Task-owned contract and evidence."""
    from harness.tasks import load_task

    task = load_task(task_id)
    root = Path(workspace)
    return EvaluationInputs(
        feature=task,
        diff=_task_scoped_diff(task, root),
        bound_test_sources=_bound_test_sources(task, root),
        effective_evidence=_current_task_evidence(task),
    )
