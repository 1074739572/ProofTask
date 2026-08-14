"""Feature store: durable state machine + atomic JSON persistence (L2).

Storage layout (per workspace, follows ``switch_workspace``)::

    <workspace>/.features/feature_<id>.json

Concurrency rules:

- every mutation is an atomic write (temp file + ``os.replace``), so a crash
  mid-write never leaves a truncated feature file;
- the whole module is process-local and intentionally lock-free: the harness
  is single-loop per workspace, and feature files are small enough that the
  read-modify-write window is negligible.  Cross-process coordination is out
  of scope for L2 (the /goal loop in L6 will serialize its own mutations).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from harness.features.schema import (
    TRANSITIONS,
    Feature,
    VerificationEvidence,
    can_transition,
)
from harness.settings import get_workspace_paths

FEATURES_DIRNAME = ".features"


def features_dir(workspace: str | Path | None = None) -> Path:
    """Directory for the given workspace (default: the active workspace)."""
    if workspace is not None:
        root = Path(workspace).expanduser().resolve()
    else:
        root = get_workspace_paths().root
    return root / FEATURES_DIRNAME


def _feature_path(feature_id: str, workspace: str | Path | None = None) -> Path:
    return features_dir(workspace) / f"{feature_id}.json"


def _load(feature_id: str, workspace: str | Path | None = None) -> Feature:
    path = _feature_path(feature_id, workspace)
    if not path.exists():
        raise FileNotFoundError(f"feature {feature_id} not found in {path.parent}")
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    return Feature(**data)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _save(feature: Feature) -> None:
    feature.updated_at = time.time()
    _atomic_write_json(_feature_path(feature.id, feature.workspace or None), feature.to_dict())


# --- Public API --------------------------------------------------------------

def create_feature(
    name: str,
    behavior: str,
    verification: str,
    *,
    workspace: str | Path | None = None,
    task_id: str | None = None,
    evaluation_required: bool = False,
    depends_on: list[str] | None = None,
    acceptance_cases: list[dict[str, Any]] | None = None,
    verification_spec: dict[str, Any] | None = None,
) -> Feature:
    """Create a feature in ``not_started`` state."""
    feature = Feature.new(
        name,
        behavior,
        verification,
        workspace=str(features_dir(workspace).parent),
        task_id=task_id,
        evaluation_required=evaluation_required,
        depends_on=depends_on or [],
        acceptance_cases=acceptance_cases or [],
        verification_spec=verification_spec or {},
    )
    _save(feature)
    return feature


def get_feature(feature_id: str, workspace: str | Path | None = None) -> Feature:
    return _load(feature_id, workspace)


def list_features(workspace: str | Path | None = None) -> list[Feature]:
    d = features_dir(workspace)
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("feat_*.json")):
        try:
            out.append(Feature(**json.loads(path.read_text(encoding="utf-8", errors="replace"))))
        except (json.JSONDecodeError, TypeError):
            continue  # never let one corrupt file break the whole board
    return out


def _transition(feature: Feature, target: str, *, error: str | None = None) -> Feature:
    if not can_transition(feature.state, target):
        raise ValueError(
            f"illegal feature transition {feature.state} -> {target} "
            f"(allowed: {sorted(TRANSITIONS.get(feature.state, set()))})"
        )
    feature.state = target
    if error is not None:
        feature.last_error = error
    if target == "passing":
        feature.completed_at = feature.completed_at or time.time()
    elif target in ("active", "failing"):
        feature.completed_at = None
    _save(feature)
    return feature


def claim_feature(feature_id: str, workspace: str | Path | None = None) -> Feature:
    """not_started -> active (agent starts working on this feature)."""
    feature = _load(feature_id, workspace)
    return _transition(feature, "active")


def block_feature(feature_id: str, reason: str, workspace: str | Path | None = None) -> Feature:
    """active/not_started -> blocked (dependency or decision missing)."""
    feature = _load(feature_id, workspace)
    return _transition(feature, "blocked", error=reason)


def reopen_feature(feature_id: str, workspace: str | Path | None = None) -> Feature:
    """blocked/failing/passing -> active (resume work on the feature)."""
    feature = _load(feature_id, workspace)
    return _transition(feature, "active")


def verify_feature(
    feature_id: str,
    passed: bool,
    evidence: VerificationEvidence | None = None,
    *,
    error: str | None = None,
    workspace: str | Path | None = None,
) -> Feature:
    """Apply a verification result.  This is the ONLY gate into ``passing``.

    Rules:
    - ``passed=True`` REQUIRES non-None evidence with ``exit_code == 0`` 鈥?
      an agent's bare claim can never flip a feature to passing;
    - a failing re-verification demotes ``passing`` back to ``failing``
      (deliberately reversible, not a one-way latch).
    """
    feature = _load(feature_id, workspace)
    if passed:
        if evidence is None:
            raise ValueError("verify_feature(passed=True) requires evidence")
        if evidence.exit_code != 0:
            raise ValueError(
                f"evidence.exit_code must be 0 for a passing verdict, got {evidence.exit_code}"
            )
        if not can_transition(feature.state, "passing"):
            raise ValueError(
                f"cannot mark {feature.state!r} feature as passing; "
                "claim or reopen it first"
            )
        feature.evidence.append(evidence.to_dict())
        feature.last_error = None
        return _transition(feature, "passing")

    # Failed verdict: active/failing -> failing, passing -> failing (reversible).
    # failing -> failing is an idempotent retry (attempts/evidence accumulate).
    if feature.state not in ("active", "failing", "passing"):
        raise ValueError(f"cannot fail a {feature.state!r} feature; claim it first")
    if evidence is not None:
        feature.evidence.append(evidence.to_dict())
    feature.attempts += 1
    return _transition(feature, "failing", error=error or "verification failed")


def record_evaluation(
    feature_id: str,
    evaluation: dict[str, Any],
    workspace: str | Path | None = None,
) -> Feature:
    """Attach an evaluator's findings to a feature (L5, advisory only).

    Never changes feature state — the machine verification (L3) remains the
    only gate into ``passing``. Stored on ``feature.evaluation``.
    """
    feature = _load(feature_id, workspace)
    feature.evaluation = evaluation
    _save(feature)
    return feature


def feature_is_stale(feature: Feature) -> bool:
    """A passing feature is stale when the code it was verified against no
    longer matches the workspace (git snapshot comparison).

    Non-git workspaces (empty snapshot) return False — cannot check there.
    """
    if feature.state != "passing" or not feature.evidence:
        return False
    snapshot = feature.evidence[-1].get("code_snapshot") or ""
    if not snapshot:
        return False
    from harness.verification.snapshot import workspace_has_changes_since

    return workspace_has_changes_since(feature.workspace or None, snapshot)


def corrupt_feature_files(workspace: str | Path | None = None) -> list[str]:
    """Names of feature files that fail to parse (strict loading for L4)."""
    d = features_dir(workspace)
    if not d.exists():
        return []
    bad: list[str] = []
    for path in sorted(d.glob("feat_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            Feature(**data)
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            bad.append(path.name)
    return bad


def clear_features(workspace: str | Path | None = None) -> int:
    """Remove all feature files (test/eval cleanup). Returns count removed."""
    d = features_dir(workspace)
    if not d.exists():
        return 0
    removed = 0
    for path in d.glob("feat_*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
