"""F-series: feature state-machine checks (L2 three-layer state model).

Deterministic, zero-LLM assertions on the NEW feature primitive
(``harness.features``):

- F001: full lifecycle  not_started -> active -> passing (with evidence)
- F002: passing REQUIRES evidence (exit_code == 0) — self-reported "done"
       has no programmatic effect on feature state
- F003: illegal transitions are rejected (e.g. not_started -> passing,
       passing -> blocked)
- F004: passing is REVERSIBLE — a failing re-verification demotes to failing
- F005: writes are atomic — a crash mid-replace never corrupts the file
- F006: task extension stays backward compatible with legacy task JSON
- F007: features are isolated per workspace (follow switch_workspace)

All tests run against temp directories; the real workspace is untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


# --- F001: full lifecycle ----------------------------------------------------

def case_f001_full_lifecycle() -> None:
    """create -> claim -> verify(passed) -> passing, with evidence recorded."""
    from harness.features import (
        VerificationEvidence,
        claim_feature,
        create_feature,
        get_feature,
        verify_feature,
    )

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature(
            "pagination fix",
            "list_users returns all pages without skipping rows",
            "pytest tests/test_pagination.py -q",
            workspace=ws,
        )
        assert feat.state == "not_started"

        feat = claim_feature(feat.id, workspace=ws)
        assert feat.state == "active"

        feat = verify_feature(
            feat.id,
            passed=True,
            evidence=VerificationEvidence(
                command="pytest tests/test_pagination.py -q",
                exit_code=0,
                stdout_tail="3 passed",
                verified_by="oracle",
            ),
            workspace=ws,
        )
        assert feat.state == "passing"
        assert feat.completed_at is not None
        assert len(feat.evidence) == 1
        assert feat.evidence[0]["exit_code"] == 0

        # State is durable: reload from disk shows the same result.
        reloaded = get_feature(feat.id, workspace=ws)
        assert reloaded.state == "passing"
        assert reloaded.evidence[0]["command"].startswith("pytest")
    finally:
        tmp.cleanup()


# --- F002: passing requires evidence -----------------------------------------

def case_f002_passing_requires_evidence() -> None:
    """A bare 'I'm done' (no evidence / bad exit code) can never set passing."""
    from harness.features import (
        VerificationEvidence,
        claim_feature,
        create_feature,
        verify_feature,
    )

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)
        claim_feature(feat.id, workspace=ws)

        # No evidence at all -> rejected.
        try:
            verify_feature(feat.id, passed=True, workspace=ws)
        except ValueError:
            pass
        else:
            raise AssertionError("verify(passed=True) without evidence must raise")

        # Evidence with non-zero exit code -> rejected.
        try:
            verify_feature(
                feat.id,
                passed=True,
                evidence=VerificationEvidence(command="pytest -q", exit_code=1),
                workspace=ws,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("verify(passed=True) with exit_code!=0 must raise")

        # State must still be active — the claims changed nothing.
        from harness.features import get_feature

        assert get_feature(feat.id, workspace=ws).state == "active"
    finally:
        tmp.cleanup()


# --- F003: illegal transitions rejected --------------------------------------

def case_f003_illegal_transitions_rejected() -> None:
    """not_started -> passing, passing -> blocked etc. are all rejected."""
    from harness.features import (
        VerificationEvidence,
        block_feature,
        claim_feature,
        create_feature,
        verify_feature,
    )

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)

        # not_started cannot jump straight to passing.
        try:
            verify_feature(
                feat.id,
                passed=True,
                evidence=VerificationEvidence(command="pytest -q", exit_code=0),
                workspace=ws,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("not_started -> passing must be rejected")

        # not_started cannot be failed either.
        try:
            verify_feature(feat.id, passed=False, workspace=ws)
        except ValueError:
            pass
        else:
            raise AssertionError("not_started -> failing must be rejected")

        # passing -> blocked is not a legal move.
        claim_feature(feat.id, workspace=ws)
        verify_feature(
            feat.id,
            passed=True,
            evidence=VerificationEvidence(command="pytest -q", exit_code=0),
            workspace=ws,
        )
        try:
            block_feature(feat.id, "nope", workspace=ws)
        except ValueError:
            pass
        else:
            raise AssertionError("passing -> blocked must be rejected")

        # A failed verification on a passing feature demotes it (reversible).
        verify_feature(feat.id, passed=False, workspace=ws)
        from harness.features import get_feature

        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- F004: reversibility -----------------------------------------------------

def case_f004_passing_reversible() -> None:
    """passing --re-verify(fail)--> failing --reopen--> active."""
    from harness.features import (
        VerificationEvidence,
        claim_feature,
        create_feature,
        get_feature,
        reopen_feature,
        verify_feature,
    )

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)
        claim_feature(feat.id, workspace=ws)
        verify_feature(
            feat.id,
            passed=True,
            evidence=VerificationEvidence(command="pytest -q", exit_code=0),
            workspace=ws,
        )
        assert get_feature(feat.id, workspace=ws).state == "passing"

        # New failing evidence rolls it back — not a one-way latch.
        verify_feature(
            feat.id,
            passed=False,
            evidence=VerificationEvidence(command="pytest -q", exit_code=2),
            error="test_pagination broke after refactor",
            workspace=ws,
        )
        feat = get_feature(feat.id, workspace=ws)
        assert feat.state == "failing"
        assert feat.attempts == 1
        assert feat.last_error == "test_pagination broke after refactor"
        assert feat.completed_at is None

        reopen_feature(feat.id, workspace=ws)
        assert get_feature(feat.id, workspace=ws).state == "active"
    finally:
        tmp.cleanup()


# --- F005: atomic writes -----------------------------------------------------

def case_f005_atomic_write_no_corruption() -> None:
    """A failure during os.replace must leave the original file intact
    and no *.tmp litter behind."""
    from harness.features import create_feature, get_feature
    from harness.features import state as feat_state

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)
        path = feat_state._feature_path(feat.id, ws)
        original = path.read_text(encoding="utf-8")

        with mock.patch("harness.features.state.os.replace", side_effect=OSError("disk full")):
            try:
                create_feature("y", "boom", "pytest -q", workspace=ws)
            except OSError:
                pass
            else:
                raise AssertionError("expected OSError from mocked replace")

        # Original file untouched.
        assert path.read_text(encoding="utf-8") == original
        # No temp litter.
        leftovers = list(path.parent.glob("*.tmp"))
        assert not leftovers, f"temp files left behind: {leftovers}"

        # The pre-existing feature still loads fine.
        assert get_feature(feat.id, workspace=ws).state == "not_started"
    finally:
        tmp.cleanup()


# --- F006: task backward compatibility ---------------------------------------

def case_f006_task_extension_backcompat() -> None:
    """Legacy task JSON (no feature_ids/attempts/last_error) still loads."""
    import harness.tasks as tasks_mod
    from harness.tasks import Task

    tmp = tempfile.TemporaryDirectory()
    try:
        with mock.patch.object(tasks_mod, "TASKS_DIR", Path(tmp.name)):
            tasks_mod.create_task("legacy subject", "desc")
            path = next(Path(tmp.name).glob("task_*.json"))
            data = json.loads(path.read_text(encoding="utf-8"))
            # Simulate a file written before the L2 extension.
            for key in ("feature_ids", "attempts", "last_error"):
                data.pop(key, None)
            path.write_text(json.dumps(data), encoding="utf-8")

            loaded = tasks_mod.load_task(data["id"])
            assert loaded.feature_ids == []
            assert loaded.attempts == 0
            assert loaded.last_error is None

            # attach_feature works on the legacy-loaded task.
            tasks_mod.attach_feature(data["id"], "feat_abc")
            loaded = tasks_mod.load_task(data["id"])
            assert loaded.feature_ids == ["feat_abc"]

            # Task(**data) also works directly with defaulted fields.
            t = Task(id="t1", subject="s", description="d", status="pending", owner=None, blockedBy=[])
            assert t.feature_ids == [] and t.attempts == 0 and t.last_error is None
    finally:
        tmp.cleanup()


# --- F007: workspace isolation -----------------------------------------------

def case_f007_workspace_isolation() -> None:
    """Features live under <workspace>/.features and never leak across workspaces."""
    from harness.features import (
        VerificationEvidence,
        claim_feature,
        create_feature,
        list_features,
        verify_feature,
    )

    tmp = tempfile.TemporaryDirectory()
    try:
        ws_a = Path(tmp.name) / "a"
        ws_b = Path(tmp.name) / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        fa = create_feature("in-a", "bhv", "pytest -q", workspace=ws_a)
        create_feature("in-b", "bhv", "pytest -q", workspace=ws_b)

        assert {f.name for f in list_features(workspace=ws_a)} == {"in-a"}
        assert {f.name for f in list_features(workspace=ws_b)} == {"in-b"}

        # Mutating A does not touch B.
        claim_feature(fa.id, workspace=ws_a)
        verify_feature(
            fa.id,
            passed=True,
            evidence=VerificationEvidence(command="pytest -q", exit_code=0),
            workspace=ws_a,
        )
        assert ws_a.joinpath(".features").exists()
        assert not ws_b.joinpath(".features", "feature_" + fa.id + ".json").exists()
    finally:
        tmp.cleanup()


# --- F008: idempotent failure retry ------------------------------------------

def case_f008_failure_retry_idempotent() -> None:
    """Repeated failed verifications must not raise (failing -> failing)."""
    from harness.features import (
        VerificationEvidence,
        claim_feature,
        create_feature,
        get_feature,
        verify_feature,
    )

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature("x", "behavior", "pytest -q", workspace=ws)
        claim_feature(feat.id, workspace=ws)
        ev = VerificationEvidence(command="pytest -q", exit_code=1)
        verify_feature(feat.id, False, ev, workspace=ws)
        verify_feature(feat.id, False, ev, workspace=ws)  # must not raise
        feat = get_feature(feat.id, workspace=ws)
        assert feat.state == "failing"
        assert feat.attempts == 2
        assert len(feat.evidence) == 2
    finally:
        tmp.cleanup()


# --- F009: stale detection ---------------------------------------------------

def case_f009_passing_stale_detected() -> None:
    """After verification, if the code changes, the passing state is stale."""
    import subprocess

    from harness.features import create_feature, feature_is_stale, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        subprocess.run(["git", "init", "-q", str(ws)], check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(ws), check=False)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=False)
        (ws / "app.py").write_text("x = 1", encoding="utf-8")
        (ws / "check.py").write_text("import sys; sys.exit(0)", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(ws), check=False)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(ws), check=False)

        feat = create_feature("x", "behavior", "python check.py", workspace=ws)
        verify_feature_command(feat.id, workspace=ws)
        assert get_feature(feat.id, workspace=ws).state == "passing"
        assert not feature_is_stale(get_feature(feat.id, workspace=ws))

        # Code changes after verification -> stale.
        (ws / "app.py").write_text("x = 2  # changed", encoding="utf-8")
        feat = get_feature(feat.id, workspace=ws)
        assert feature_is_stale(feat)
    finally:
        tmp.cleanup()


# --- F010: corrupt feature files reported ------------------------------------

def case_f010_corrupt_feature_files() -> None:
    """corrupt_feature_files reports unreadable files (strict loading for L4)."""
    from harness.features import corrupt_feature_files

    tmp, ws = _tmp_workspace()
    try:
        (ws / ".features").mkdir()
        (ws / ".features" / "feat_bad.json").write_text("{broken", encoding="utf-8")
        bad = corrupt_feature_files(ws)
        assert "feat_bad.json" in bad
    finally:
        tmp.cleanup()


CASES = [
    EvalCase(
        "f001.feature_full_lifecycle",
        "F001: feature lifecycle not_started->active->passing with evidence",
        "features",
        case_f001_full_lifecycle,
    ),
    EvalCase(
        "f002.passing_requires_evidence",
        "F002: passing requires verification evidence (no self-claim)",
        "features",
        case_f002_passing_requires_evidence,
    ),
    EvalCase(
        "f003.illegal_transitions_rejected",
        "F003: illegal feature transitions are rejected",
        "features",
        case_f003_illegal_transitions_rejected,
    ),
    EvalCase(
        "f004.passing_reversible",
        "F004: passing demotes to failing on failed re-verification",
        "features",
        case_f004_passing_reversible,
    ),
    EvalCase(
        "f005.atomic_write",
        "F005: feature writes are atomic (no corruption on crash)",
        "features",
        case_f005_atomic_write_no_corruption,
    ),
    EvalCase(
        "f006.task_extension_backcompat",
        "F006: task extension keeps legacy JSON loadable",
        "features",
        case_f006_task_extension_backcompat,
    ),
    EvalCase(
        "f007.workspace_isolation",
        "F007: features are isolated per workspace",
        "features",
        case_f007_workspace_isolation,
    ),
    EvalCase(
        "f008.failure_retry_idempotent",
        "F008: repeated failed verifications are idempotent (no raise)",
        "features",
        case_f008_failure_retry_idempotent,
    ),
    EvalCase(
        "f009.passing_stale_detected",
        "F009: passing state is stale after the code changes",
        "features",
        case_f009_passing_stale_detected,
    ),
    EvalCase(
        "f010.corrupt_feature_files",
        "F010: corrupt feature files are reported for strict loading",
        "features",
        case_f010_corrupt_feature_files,
    ),
]
