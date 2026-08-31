"""Deterministic verification gate (L3).

The bridge between the feature store (L2) and controlled command execution:

- a feature declares its verification (``feature.verification``);
- :func:`verify_feature_command` runs that declared command under the
  policy + permission gates and updates the feature state through the ONLY
  gate into ``passing`` (``harness.features.verify_feature``).

Layer map (per the reliability plan §4):

- ``policy.py``  — what may run as a verification command (structural gate);
- ``runner.py``  — controlled execution (cwd / stdin / timeout / output);
- ``evidence.py`` — build :class:`VerificationEvidence` from a run result;
- feature store (``harness.features``) — durable state + evidence record.

Public API::

    verify_feature_command(feature_id, *, workspace, timeout_s) -> Feature
    run_verification(command, *, workspace, timeout_s) -> VerificationRunResult
    check_verification_command(command) -> VerificationDecision
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from harness.features import (
    Feature,
    claim_feature,
    get_feature,
    verify_feature,
)
from harness.verification.evidence import diagnose_verification_output, evidence_from_result
from harness.verification.catalog import (
    TestCatalog,
    build_pytest_command,
    collect_pytest_catalog,
    parse_pytest_collection_output,
)
from harness.verification.policy import (
    ALLOWED_PROGRAMS,
    DENY_TOKENS,
    GIT_READONLY_SUBCOMMANDS,
    MAX_VERIFICATION_COMMAND_LEN,
    VerificationDecision,
    check_verification_command,
)
from harness.verification.runner import (
    DEFAULT_VERIFY_TIMEOUT_S,
    VerificationRunResult,
    run_verification,
)
from harness.verification.adapters import VerificationAdapter, VerificationContext
from harness.verification.node_adapter import NodeTestAdapter, NodeTestCatalog
from harness.verification.pytest_adapter import PytestAdapter
from harness.verification.registry import select_adapter

__all__ = [
    "ALLOWED_PROGRAMS",
    "DENY_TOKENS",
    "GIT_READONLY_SUBCOMMANDS",
    "MAX_VERIFICATION_COMMAND_LEN",
    "DEFAULT_VERIFY_TIMEOUT_S",
    "Feature",
    "TestCatalog",
    "VerificationDecision",
    "VerificationRunResult",
    "diagnose_verification_output",
    "check_verification_command",
    "build_pytest_command",
    "collect_pytest_catalog",
    "parse_pytest_collection_output",
    "run_verification",
    "verify_feature_command",
    "verify_task_command",
    "reverify_task_command",
    "VerificationAdapter",
    "VerificationContext",
    "PytestAdapter",
    "NodeTestAdapter",
    "NodeTestCatalog",
    "select_adapter",
]


def verify_feature_command(
    feature_id: str,
    *,
    workspace: str | Path | None = None,
    timeout_s: float | None = None,
) -> Feature:
    """Run a feature's declared verification and update its state.

    Behavior:

    - a ``not_started`` feature is claimed first (auto-activate) so a fresh
      feature can be verified immediately;
    - policy/permission rejections do NOT raise — they mark the feature
      ``failing`` with the rejection reason (the state machine stays the
      single source of truth);
    - a successful run (exit 0, no timeout) records evidence and flips the
      feature to ``passing`` via ``harness.features.verify_feature`` — the
      only path that may set ``passing``.
    """
    workspace = Path(workspace).expanduser().resolve() if workspace else None
    feature: Feature = get_feature(feature_id, workspace)
    if feature.state == "not_started":
        feature = claim_feature(feature_id, workspace)

    result = run_verification(
        feature.verification,
        workspace=feature.workspace or workspace,
        timeout_s=timeout_s,
    )

    error: str | None = None
    if result.error is not None:
        error = result.error
    elif result.exit_code != 0:
        error = f"verification failed with exit code {result.exit_code}"

    # Policy/permission rejections and workspace-mutation failures never
    # executed successfully — record the reason, no fake evidence. Timeouts
    # keep evidence (exit 124 convention).
    executed = result.error is None or result.timed_out
    spec = feature.verification_spec if isinstance(feature.verification_spec, dict) else {}
    selectors = spec.get("selectors") or []
    evidence = (
        evidence_from_result(
            result,
            workspace=str(feature.workspace or workspace),
            selectors=selectors if isinstance(selectors, list) else (),
            collected_count=spec.get("collected_count") or 0,
        )
        if executed
        else None
    )

    return verify_feature(
        feature_id,
        passed=result.passed,
        evidence=evidence,
        error=error,
        workspace=workspace,
    )


def verify_task_command(
    task_id: str,
    *,
    workspace: str | Path | None = None,
    timeout_s: float | None = None,
    cancel_check=None,
    controller_authorized: bool = False,
):
    """Run a Task's bound selector command and persist its machine verdict."""
    from harness.tasks import load_task, save_task, set_task_verification_result

    task = load_task(task_id)
    if _migrate_task_runner_to_bun(task, workspace):
        save_task(task)
    passed, evidence, error = _run_task_verification(
        task, workspace=workspace, timeout_s=timeout_s, cancel_check=cancel_check,
        controller_authorized=controller_authorized,
    )
    return set_task_verification_result(task_id, passed=passed, evidence=evidence, error=error)


def reverify_task_command(
    task_id: str,
    *,
    workspace: str | Path | None = None,
    timeout_s: float | None = None,
    cancel_check=None,
    controller_authorized: bool = False,
):
    """Re-run a completed Task's binding during the final Goal gate."""
    from harness.tasks import load_task, record_task_reverification, save_task

    task = load_task(task_id)
    if _migrate_task_runner_to_bun(task, workspace):
        save_task(task, archived=task.status == "completed")
    passed, evidence, error = _run_task_verification(
        task, workspace=workspace, timeout_s=timeout_s, cancel_check=cancel_check,
        controller_authorized=controller_authorized,
    )
    return record_task_reverification(task_id, passed=passed, evidence=evidence, error=error)


def _migrate_task_runner_to_bun(task, workspace: str | Path | None) -> bool:
    """Upgrade old Node+tsx bindings when this workspace vendors Bun.

    Generated Task contracts are durable.  Updating adapter selection alone
    would leave a paused Goal retrying an already-known incompatible command.
    This narrowly replaces only the legacy Node test prefix, preserving the
    exact frozen test files and selectors.
    """
    if workspace is None or not isinstance(task.verification_spec, dict):
        return False
    spec = task.verification_spec
    if str(spec.get("adapter") or "").lower() != "node":
        return False
    command = str(spec.get("command") or "").strip()
    prefix = "node --import tsx --test"
    if command != prefix and not command.startswith(prefix + " "):
        return False
    root = Path(workspace).expanduser().resolve()
    bun = root / "node_modules" / "@oven" / "bun-windows-x64" / "bin" / "bun.exe"
    if not bun.is_file():
        return False
    suffix = command[len(prefix):].strip()
    spec["command"] = "./node_modules/@oven/bun-windows-x64/bin/bun.exe test" + (f" {suffix}" if suffix else "")
    task.verification_spec = spec
    return True


def _run_task_verification(
    task,
    *,
    workspace: str | Path | None = None,
    timeout_s: float | None = None,
    cancel_check=None,
    controller_authorized: bool = False,
) -> tuple[bool, dict | None, str | None]:
    """Execute one Task binding without assuming the Task is active."""
    spec = task.verification_spec
    command = str(spec.get("command") or "").strip()
    selectors = spec.get("selectors") or []
    collected_count = int(spec.get("collected_count") or 0)
    if not command or not selectors or collected_count <= 0:
        return False, None, "task has no collected verification binding; generate and baseline tests first"
    root = Path(workspace).expanduser().resolve() if workspace else None
    expected_hashes = spec.get("test_hashes") if isinstance(spec.get("test_hashes"), dict) else {}
    if root is not None and expected_hashes:
        for rel, expected in expected_hashes.items():
            try:
                current = hashlib.sha256((root / str(rel)).read_bytes()).hexdigest()
            except OSError as exc:
                return False, None, f"bound test file is unavailable: {rel}: {exc}"
            if current != str(expected):
                return False, None, f"bound test file changed after it was approved: {rel}"
    from harness.verification.snapshot import capture_code_snapshot

    snapshot_before = capture_code_snapshot(root)
    result = run_verification(
        command, workspace=root, timeout_s=timeout_s, cancel_check=cancel_check,
        controller_authorized=controller_authorized,
    )
    snapshot_after = capture_code_snapshot(root)
    diagnostics = diagnose_verification_output(result.stdout or "", selectors=selectors)
    error = result.error or (None if result.passed else f"verification failed with exit code {result.exit_code}")
    if not result.passed and diagnostics.get("failure_mode") == "common_runtime_error":
        summary = diagnostics.get("result_summary") or {}
        common = str(diagnostics.get("common_failure") or "common runtime error")[:500]
        failed_cases = diagnostics.get("failed_cases") or []
        affected = f"; affected cases: {len(failed_cases)}" if failed_cases else ""
        case_names = ", ".join(str(name)[:120] for name in failed_cases[:8])
        case_detail = f"; cases: {case_names}" if case_names else ""
        error = (
            f"verification blocked before assertions: {common} "
            f"({summary.get('passed', 0)} pass / {summary.get('failed', 0)} fail{affected}{case_detail})"
        )
    executed = result.error is None or result.timed_out
    evidence = (
        evidence_from_result(
            result,
            workspace=str(root or ""),
            selectors=selectors,
            collected_count=collected_count,
        ).to_dict()
        if executed
        else None
    )
    # Every executed Task verification gets a durable, source-locatable debug
    # bundle.  The bundle is advisory evidence; the exit code remains the
    # only machine verdict.  Writing under .project is excluded from code
    # snapshots, so diagnostics cannot make a read-only test look mutated.
    if executed:
        try:
            from harness.verification.debug import build_debug_bundle, write_debug_bundle

            bundle = build_debug_bundle(
                result,
                workspace=root,
                goal_id=str(getattr(task, "goal_id", "") or ""),
                task_id=str(getattr(task, "id", "") or ""),
                phase="task_verify",
                selectors=selectors,
                test_files=spec.get("test_files") or [],
                acceptance_ids=[
                    str(case.get("id"))
                    for case in (getattr(task, "acceptance_cases", []) or [])
                    if isinstance(case, dict) and case.get("id")
                ],
                approved_paths=[
                    *(getattr(task, "primary_write", []) or []),
                    *(getattr(task, "planned_new", []) or []),
                ],
                control_group=spec.get("control_group") if isinstance(spec.get("control_group"), dict) else None,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
            )
            paths = write_debug_bundle(bundle, workspace=root)
            if evidence is not None:
                evidence_diagnostics = evidence.setdefault("diagnostics", {})
                evidence_diagnostics["debug_bundle"] = paths
                # Promote the debug classifier into the durable evidence so
                # Goal repair routing does not need to reopen bundle files or
                # infer an invalid test contract from a plain 1-failure exit.
                failure = bundle.get("failure") if isinstance(bundle.get("failure"), dict) else {}
                contract = bundle.get("contract") if isinstance(bundle.get("contract"), dict) else {}
                evidence_diagnostics["debug_failure_category"] = str(failure.get("category") or "")
                evidence_diagnostics["debug_underlying_category"] = str(failure.get("underlying_category") or "")
                evidence_diagnostics["verification_contract_status"] = str(contract.get("status") or "")
                evidence_diagnostics["verification_contract_invalid"] = bool(contract.get("invalid"))
                issues = contract.get("issues") if isinstance(contract.get("issues"), list) else []
                evidence_diagnostics["verification_contract_issues"] = [
                    {
                        "code": str(item.get("code") or "")[:100],
                        "message": str(item.get("message") or "")[:500],
                        "severity": str(item.get("severity") or "")[:30],
                    }
                    for item in issues[:8]
                    if isinstance(item, dict)
                ]
                evidence["debug_bundle"] = paths
        except Exception as exc:  # pragma: no cover - diagnostics must not gate verification
            if evidence is not None:
                evidence.setdefault("diagnostics", {})["debug_bundle_error"] = f"{type(exc).__name__}: {exc}"
    return result.passed, evidence, error
