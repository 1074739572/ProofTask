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


@dataclass
class EvaluationInputs:
    feature: Feature
    diff: str
    rubric: list[str] = field(default_factory=lambda: list(RUBRIC))

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
        if self.feature.evidence:
            for ev in self.feature.evidence:
                lines.append(
                    f"- command: {ev.get('command', '')} | "
                    f"exit_code: {ev.get('exit_code')} | "
                    f"verified_by: {ev.get('verified_by', '')} | "
                    f"collected_count: {ev.get('collected_count', 0)}"
                )
                selectors = ev.get("selectors") or []
                if selectors:
                    lines.append(f"  selectors: {', '.join(str(item) for item in selectors[:8])}")
                tail = (ev.get("stdout_tail") or "").strip()
                if tail:
                    lines.append(f"  stdout_tail: {tail[:500]}")
        else:
            lines.append("(无证据 — 该 feature 尚未通过机器验证)")
        lines += ["", "# 实际改动 (git diff)", self.diff or "(无 diff 或非 git 仓库)", ""]
        lines += ["# 评分标准", *[f"- {r}" for r in self.rubric]]
        return "\n".join(lines)


def _git_diff(workspace: Path) -> str:
    """Diff of tracked changes PLUS the full content of untracked files —
    an evaluator must see new files, not just modifications."""
    parts: list[str] = []
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
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
            proc = subprocess.run(
                ["git", "diff"],
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
                if rel:
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


def collect_inputs(feature_id: str, workspace: str | Path | None = None) -> EvaluationInputs:
    """Assemble the inputs for evaluating one feature."""
    feature: Feature = get_feature(feature_id, workspace)
    ws = Path(feature.workspace or workspace or ".")
    return EvaluationInputs(feature=feature, diff=_git_diff(ws))


def collect_task_inputs(task_id: str, workspace: str | Path) -> EvaluationInputs:
    """Assemble evaluator inputs from a Task-owned contract and evidence."""
    from harness.tasks import load_task

    task = load_task(task_id)
    return EvaluationInputs(feature=task, diff=_git_diff(Path(workspace)))
