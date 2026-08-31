"""Deterministic diagnostics for a verification run.

The test runner remains the source of truth for pass/fail.  This module adds
the missing evidence layer: phase classification, failed-case extraction,
stack/source context, and a small static check for obviously contradictory
generated tests.  It never changes a Task or a test file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from harness.verification.evidence import diagnose_verification_output
from harness.verification.runner import VerificationRunResult, run_verification

DEBUG_SCHEMA_VERSION = 1
MAX_FRAMES = 24
MAX_CONTEXT_LINES = 5
MAX_ISSUES = 20

_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^()\r\n\s]*?):(?P<line>\d+):(?P<column>\d+)"
)
_TEST_DECL_RE = re.compile(r"\b(?:test|it)\s*\(\s*(['\"])(?P<name>.*?)\1", re.DOTALL)
_PYTEST_DECL_RE = re.compile(r"^\s*def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_CASE_ID_RE = re.compile(r"\b(?:AC|OV|OF|OC|R)\d+[A-Z]*\b", re.IGNORECASE)
_CONST_STRING_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)


def extract_interaction_trace(output: str) -> dict[str, Any] | None:
    """Read structured UI interaction evidence emitted by a test on failure."""
    for line in reversed(str(output or "").splitlines()):
        if "interaction_trace" not in line:
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("interaction_trace"), list):
            return value
    return None


def _repo_root(workspace: Path) -> Path:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return workspace


def _normalise_path(value: str) -> str:
    value = str(value or "").strip().strip("()[]{}.,")
    if value.startswith("file://"):
        value = value[7:]
    return value.replace("\\", "/")


def _resolve_frame_path(raw: str, workspace: Path) -> tuple[str, Path | None]:
    value = _normalise_path(raw)
    candidates: list[Path] = []
    path = Path(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", value):
        candidates.append(path)
    else:
        candidates.append(workspace / value)
        candidates.append(_repo_root(workspace) / value)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            try:
                return resolved.relative_to(workspace).as_posix(), resolved
            except ValueError:
                return value, resolved
    return value, None


def extract_stack_frames(output: str, workspace: str | Path | None = None) -> list[dict[str, Any]]:
    """Extract source locations from JS/TS and Python style stack traces."""
    root = Path(workspace or os.getcwd()).expanduser().resolve()
    frames: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for raw_line in str(output or "").splitlines():
        for match in _LOCATION_RE.finditer(raw_line):
            raw_path = match.group("path")
            line = int(match.group("line"))
            column = int(match.group("column"))
            path, resolved = _resolve_frame_path(raw_path, root)
            key = (path, line, column)
            if key in seen:
                continue
            seen.add(key)
            frames.append({
                "path": path,
                "line": line,
                "column": column,
                "source_line": raw_line.strip()[:500],
                "in_workspace": bool(resolved and resolved.is_relative_to(root)),
            })
            if len(frames) >= MAX_FRAMES:
                return frames
    return frames


def source_context(
    frames: Iterable[dict[str, Any]],
    workspace: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read a bounded context window for frames inside the workspace."""
    root = Path(workspace or os.getcwd()).expanduser().resolve()
    result: list[dict[str, Any]] = []
    for frame in frames:
        if not frame.get("in_workspace"):
            continue
        try:
            path = (root / str(frame["path"])).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            # Dependency internals explain the mechanism, but are not
            # actionable Task source. Keep them in the stack only.
            if "node_modules" in path.parts:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            number = int(frame["line"])
        except (OSError, TypeError, ValueError):
            continue
        start = max(1, number - MAX_CONTEXT_LINES)
        end = min(len(lines), number + MAX_CONTEXT_LINES)
        result.append({
            "path": path.relative_to(root).as_posix(),
            "line": number,
            "context": [f"{index}: {lines[index - 1]}" for index in range(start, end + 1)],
        })
    return result[:MAX_FRAMES]


def _test_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(_TEST_DECL_RE.finditer(text)) + list(_PYTEST_DECL_RE.finditer(text))
    matches.sort(key=lambda match: match.start())
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((str(match.group("name") or "").strip(), text[match.start():end]))
    return blocks


def lint_test_contract(
    test_files: Iterable[str],
    *,
    workspace: str | Path | None = None,
    acceptance_ids: Iterable[str] = (),
    require_control_group: bool | None = None,
) -> dict[str, Any]:
    """Find high-signal contract mistakes before a failure enters repair.

    This is deliberately conservative.  It reports suspicious evidence and
    only marks a contract invalid for contradictions that are unambiguous in
    the test source (for example, a visible option plus a no-border assertion
    in the same test case).
    """
    root = Path(workspace or os.getcwd()).expanduser().resolve()
    expected_ids = {str(item).upper() for item in acceptance_ids if str(item).strip()}
    issues: list[dict[str, Any]] = []
    test_names: list[str] = []
    detected_ids: set[str] = set()
    saw_render_test = False
    saw_control_marker = False
    for raw_file in test_files:
        value = _normalise_path(raw_file)
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append({"severity": "high", "code": "test_file_unavailable", "message": f"{value}: {exc}"})
            continue
        if path.suffix.lower() in {".tsx", ".jsx"} or "@opentui" in text or "render(" in text:
            saw_render_test = True
        if re.search(r"\b(?:baseline|control_group|controlGroup|withOverlay|withoutOverlay|renderBaseline|renderTarget)\b", text, re.IGNORECASE):
            saw_control_marker = True
        # A GoalSummary with one task intentionally renders that task's
        # subject on the collapsed frame. Reusing the same constant as both
        # ``subject`` and a marker that must be absent makes the test
        # contradictory before any product behavior is exercised.
        constants = {
            match.group("name"): match.group("value")
            for match in _CONST_STRING_RE.finditer(text)
            if match.group("value").strip()
        }
        for name, marker in constants.items():
            subject_ref = re.search(rf"\bsubject\s*:\s*{re.escape(name)}\b", text)
            hidden_ref = re.search(
                rf"\b(?:doesNotMatch|not\.includes)\s*\([\s\S]{{0,500}}\b{re.escape(name)}\b",
                text,
            )
            if subject_ref and hidden_ref:
                issues.append({
                    "severity": "high",
                    "code": "hidden_marker_is_summary_subject",
                    "message": (
                        f"marker {name} ({marker!r}) is also the current Task subject but the test asserts it is absent "
                        "from the collapsed frame"
                    ),
                })
        for name, block in _test_blocks(text):
            test_names.append(name)
            detected_ids.update(item.upper() for item in _CASE_ID_RE.findall(name))
            if re.search(r"VISIBLE_OPTION", block) and re.search(r"doesNotMatch\s*\([^\n]*BORDER", block):
                issues.append({
                    "severity": "high",
                    "code": "contradictory_visibility_assertion",
                    "test": name,
                    "message": "the case creates a visible option but also asserts that no border is rendered",
                })
    if expected_ids:
        unexpected = sorted(detected_ids - expected_ids)
        if unexpected:
            issues.append({
                "severity": "high",
                "code": "acceptance_case_mismatch",
                "message": f"test mentions cases outside the Task contract: {', '.join(unexpected)}",
                "expected": sorted(expected_ids),
                "detected": sorted(detected_ids),
            })
    if require_control_group is None:
        require_control_group = saw_render_test
    if require_control_group and not saw_control_marker:
        issues.append({
            "severity": "medium",
            "code": "control_group_not_declared",
            "message": "rendering test has no explicit baseline/control-group marker",
        })
    if not test_names:
        issues.append({"severity": "high", "code": "no_collectable_test", "message": "no test declaration was found"})
    high = [item for item in issues if item.get("severity") == "high"]
    return {
        "status": "invalid_verification_contract" if high else "ok",
        "invalid": bool(high),
        "test_names": test_names[:100],
        "detected_case_ids": sorted(detected_ids),
        "issues": issues[:MAX_ISSUES],
    }


def _classify_failure(result: VerificationRunResult, diagnostics: dict[str, Any]) -> tuple[str, str, str]:
    error = str(result.error or "").lower()
    output = str(result.stdout or "")
    if result.timed_out or "timed out" in error:
        return "timeout", "high", "verification command exceeded its deadline"
    if any(marker in error for marker in ("permission", "policy rejected", "failed to start", "cancelled")):
        return "external_unavailable", "high", str(result.error)
    common = str(diagnostics.get("common_failure") or "").lower()
    has_runtime = bool(common)
    has_assertion = bool(diagnostics.get("expected_actual") or diagnostics.get("failure_mode") == "case_assertions")
    if has_runtime and has_assertion:
        return "mixed_runtime_and_assertion", "high", "the run contains both a runtime failure and a behavior assertion failure"
    if diagnostics.get("blocked_before_assertions") or has_runtime:
        if "orphan text" in common or "render" in common:
            return "runtime_render", "high", str(diagnostics.get("common_failure") or "rendering failed")
        return "runtime_error", "high", str(diagnostics.get("common_failure") or "runtime failed")
    if has_assertion:
        return "behavior_assertion", "high", "the test reached a behavior assertion"
    if re.search(r"(?:syntaxerror|cannot find module|modulenotfounderror|error collecting)", output, re.IGNORECASE):
        return "test_load_error", "high", "test loading or collection failed"
    if result.passed:
        return "pass", "none", "verification exited successfully"
    return "unknown_failure", "medium", "non-zero verification result without a recognized failure shape"


def _changed_files(workspace: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"], cwd=str(workspace), capture_output=True,
            text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=5,
        )
        if proc.returncode != 0:
            return []
        return [line[3:].strip() for line in proc.stdout.splitlines() if len(line) >= 4][:100]
    except (OSError, subprocess.TimeoutExpired):
        return []


def build_debug_bundle(
    result: VerificationRunResult,
    *,
    workspace: str | Path | None = None,
    goal_id: str = "",
    task_id: str = "",
    phase: str = "verify",
    selectors: Iterable[str] = (),
    test_files: Iterable[str] = (),
    acceptance_ids: Iterable[str] = (),
    approved_paths: Iterable[str] = (),
    control_group: dict[str, Any] | None = None,
    snapshot_before: str = "",
    snapshot_after: str = "",
) -> dict[str, Any]:
    root = Path(workspace or os.getcwd()).expanduser().resolve()
    selector_list = [str(item) for item in selectors if str(item).strip()]
    test_file_list = [str(item) for item in test_files if str(item).strip()]
    diagnostics = diagnose_verification_output(result.stdout or "", selectors=selector_list)
    interaction = extract_interaction_trace(result.stdout or "")
    if interaction:
        diagnostics["interaction"] = interaction
    # Bun prints lowercase `actual`/`expected`, while node:test and pytest
    # usually capitalize the labels. Normalize both forms into one field.
    if not diagnostics.get("expected_actual"):
        expected = re.search(r"\bexpected:\s*([^\r\n]+)", result.stdout or "", re.IGNORECASE)
        actual = re.search(r"\b(?:actual|received):\s*([^\r\n]+)", result.stdout or "", re.IGNORECASE)
        if expected and actual:
            diagnostics["expected_actual"] = {
                "expected": expected.group(1).strip().strip(",")[:500],
                "actual": actual.group(1).strip().strip(",")[:500],
            }
    frames = extract_stack_frames(result.stdout or "", root)
    contract = lint_test_contract(
        test_file_list,
        workspace=root,
        acceptance_ids=acceptance_ids,
    ) if test_file_list else {"status": "not_checked", "invalid": False, "issues": []}
    category, severity, reason = _classify_failure(result, diagnostics)
    if diagnostics.get("failure_mode") == "test_observation_gap":
        category, severity, reason = "test_contract_review", "high", "component state changed but the test renderer did not expose the updated frame"
    underlying_category = category
    if contract.get("invalid"):
        category, severity, reason = "invalid_verification_contract", "high", "test source contains an unambiguous contract contradiction"
    signature_payload = {
        "category": category,
        "common_failure": diagnostics.get("common_failure") or "",
        "failed_cases": diagnostics.get("failed_cases") or [],
        "expected_actual": diagnostics.get("expected_actual") or {},
        "frames": [(frame["path"], frame["line"], frame["column"]) for frame in frames[:8]],
    }
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    now = time.time()
    return {
        "schema_version": DEBUG_SCHEMA_VERSION,
        "run_id": f"verify_{int(now)}_{uuid.uuid4().hex[:8]}",
        "created_at": now,
        "goal_id": str(goal_id or ""),
        "task_id": str(task_id or ""),
        "phase": phase,
        "workspace": str(root),
        "command": result.command,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": round(float(result.duration_ms), 1),
        "error": result.error,
        "diagnostics": diagnostics,
        "failure": {
            "category": category,
            "underlying_category": underlying_category,
            "severity": severity,
            "reason": reason,
            "signature": signature,
            "failed_cases": diagnostics.get("failed_cases", []),
            "expected_actual": diagnostics.get("expected_actual", {}),
        },
        "stack": frames,
        "source_context": source_context(frames, root),
        "contract": contract,
        "selectors": selector_list,
        "test_files": test_file_list,
        "approved_paths": [str(item) for item in approved_paths if str(item).strip()],
        "snapshots": {
            "before": snapshot_before,
            "after": snapshot_after,
            "changed_files": _changed_files(root),
        },
        "control_group": control_group or {"status": "not_provided"},
        "interaction_trace": interaction or {"status": "not_emitted"},
        "reproduce": {"command": result.command, "cwd": str(root)},
        "output_tail": (result.stdout or "")[-8_000:],
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_debug_bundle(bundle: dict[str, Any], *, workspace: str | Path | None = None) -> dict[str, str]:
    root = _repo_root(Path(workspace or os.getcwd()).expanduser().resolve())
    directory = root / ".project" / "verification-debug" / str(bundle.get("run_id") or "unknown")
    json_path = directory / "bundle.json"
    markdown_path = directory / "report.md"
    _atomic_write(json_path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(markdown_path, format_debug_report(bundle))
    return {"directory": str(directory), "json": str(json_path), "markdown": str(markdown_path)}


def format_debug_report(bundle: dict[str, Any]) -> str:
    failure = bundle.get("failure") if isinstance(bundle.get("failure"), dict) else {}
    diagnostics = bundle.get("diagnostics") if isinstance(bundle.get("diagnostics"), dict) else {}
    lines = [
        f"# Verification Debug {bundle.get('run_id', '')}",
        "",
        f"- Task: `{bundle.get('task_id') or '(none)'}`",
        f"- Phase: `{bundle.get('phase')}`",
        f"- Category: `{failure.get('category')}` ({failure.get('severity')})",
        f"- Underlying category: `{failure.get('underlying_category') or '(none)'}`",
        f"- Signature: `{failure.get('signature')}`",
        f"- Command: `{bundle.get('command')}`",
        f"- Exit: `{bundle.get('exit_code')}`; timed out: `{bundle.get('timed_out')}`",
        f"- Reason: {failure.get('reason')}",
        f"- Common failure: {diagnostics.get('common_failure') or '(none detected)'}",
        f"- Before assertions: `{diagnostics.get('blocked_before_assertions')}`",
        "",
        "## Cases",
        "",
    ]
    if diagnostics.get("test_observation_gap"):
        lines += ["", "- Observation gap: component state changed, but the test renderer did not expose the updated frame.", "- Route: `test_contract_review` (do not enter implementation repair)."]
    cases = diagnostics.get("failed_cases") or []
    if cases:
        lines.extend(f"- {case}" for case in cases)
    else:
        lines.append("- none")
    lines += ["", "## Expected / Actual", ""]
    expected_actual = failure.get("expected_actual") or {}
    if expected_actual:
        lines += [f"- Expected: `{expected_actual.get('expected', '')}`", f"- Actual: `{expected_actual.get('actual', '')}`"]
    else:
        lines.append("- not available")
    interaction = bundle.get("interaction_trace") if isinstance(bundle.get("interaction_trace"), dict) else {}
    lines += ["", "## Interaction Trace", ""]
    if interaction.get("classification"):
        lines.append(f"- Classification: `{interaction.get('classification')}`")
    trace = interaction.get("interaction_trace")
    if isinstance(trace, list) and trace:
        for event in trace[-20:]:
            if isinstance(event, dict):
                state = event.get("state_after") or event.get("state_before") or "-"
                lines.append(f"- `{event.get('event')}` target=`{event.get('target')}` state=`{state}`")
    else:
        lines.append("- No component interaction trace emitted.")
    lines += ["", "## Stack", ""]
    for frame in bundle.get("stack") or []:
        lines.append(f"- `{frame.get('path')}:{frame.get('line')}:{frame.get('column')}`")
    if not bundle.get("stack"):
        lines.append("- no source frame found")
    lines += ["", "## Source Context", ""]
    contexts = bundle.get("source_context") or []
    if contexts:
        for item in contexts[:4]:
            lines.append(f"- `{item.get('path')}:{item.get('line')}`")
            lines.extend(f"  {line}" for line in (item.get("context") or [])[:11])
    else:
        lines.append("- no application/test source context found")
    contract = bundle.get("contract") if isinstance(bundle.get("contract"), dict) else {}
    lines += ["", "## Contract", "", f"- Status: `{contract.get('status', 'not_checked')}`"]
    for issue in contract.get("issues") or []:
        lines.append(f"- [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
    lines += ["", "## Reproduce", "", f"```text\ncd {bundle.get('workspace')}\n{bundle.get('command')}\n```", ""]
    return "\n".join(lines)


def _load_task_args(task_id: str, workspace: Path) -> tuple[str, str, list[str], list[str], list[str], list[str], str]:
    from harness.tasks import load_task

    task = load_task(task_id)
    spec = task.verification_spec or {}
    return (
        str(spec.get("command") or ""),
        str(getattr(task, "goal_id", "") or ""),
        list(spec.get("selectors") or []),
        list(spec.get("test_files") or []),
        [str(case.get("id")) for case in task.acceptance_cases if isinstance(case, dict) and case.get("id")],
        [*(getattr(task, "primary_write", []) or []), *(getattr(task, "planned_new", []) or [])],
        str(getattr(task, "subject", "") or ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one verification command and write a diagnostic bundle.")
    parser.add_argument("--command", help="bound verification command")
    parser.add_argument("--task-id", help="load command and scope from a Task")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--goal-id", default="")
    parser.add_argument("--phase", default="verify")
    parser.add_argument("--selector", action="append", default=[])
    parser.add_argument("--test-file", action="append", default=[])
    parser.add_argument("--acceptance-id", action="append", default=[])
    parser.add_argument("--approved-path", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    task_id = str(args.task_id or "")
    subject = ""
    if task_id:
        command, goal_id, selectors, test_files, acceptance_ids, approved, subject = _load_task_args(task_id, workspace)
        args.command = args.command or command
        args.goal_id = args.goal_id or goal_id
        args.selector = list(dict.fromkeys([*selectors, *args.selector]))
        args.test_file = list(dict.fromkeys([*test_files, *args.test_file]))
        args.acceptance_id = list(dict.fromkeys([*acceptance_ids, *args.acceptance_id]))
        args.approved_path = list(dict.fromkeys([*approved, *args.approved_path]))
    command = str(args.command or "").strip()
    if command:
        result = run_verification(command, workspace=workspace, timeout_s=args.timeout, controller_authorized=True)
    else:
        result = VerificationRunResult("", None, "", False, 0.0, error="no verification command supplied")
    bundle = build_debug_bundle(
        result,
        workspace=workspace,
        goal_id=args.goal_id,
        task_id=task_id,
        phase=args.phase,
        selectors=args.selector,
        test_files=args.test_file,
        acceptance_ids=args.acceptance_id,
        approved_paths=args.approved_path,
    )
    paths = write_debug_bundle(bundle, workspace=workspace)
    print(format_debug_report(bundle))
    print(json.dumps(paths, ensure_ascii=False))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
