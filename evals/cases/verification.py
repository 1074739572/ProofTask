"""V-series: deterministic verification gate checks (L3).

Zero-LLM assertions on ``harness.verification`` — the bridge that runs a
feature's declared verification under policy + permission gates and updates
feature state:

- V001: destructive commands / shell metacharacters / python -c are rejected
- V002: deterministic commands pass the policy; git is read-only
- V003: successful run -> evidence (exit 0) -> feature passing
- V004: failing run (exit != 0) -> feature failing + last_error
- V005: timeout kills the process -> feature failing, no hang
- V006: permission engine (non-allow) blocks the run
- V007: long output is trimmed in evidence
- V008: policy rejection marks failing (no fake evidence), never raises
- V009: a verification command that modifies the workspace is reported failed
- V010: script path must exist inside the workspace

All tests run repository scripts written into temp workspaces.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


def _write_script(ws: Path, name: str, code: str) -> None:
    (ws / name).write_text(code, encoding="utf-8")


# --- V001 / V002: policy gate ------------------------------------------------

def case_v001_policy_rejects_destructive() -> None:
    from harness.verification.policy import check_verification_command

    for bad in (
        "rm -rf .",
        "rmdir ./x",
        "sudo pytest -q",
        "mv a b",
        "pytest -q > out.txt",
        "pytest -q >> log.txt",
        "chmod +x app.py",
        # shell metacharacters / operators — never through a shell
        "pytest -q && rm -rf .",
        "pytest -q; rm x",
        "pytest -q | grep x",
        "pytest -q &",
        "python check.py `pwd`",
        "python check.py $(ls)",
        # python -c is arbitrary inline code — rejected. The Goal's explicit
        # python -m pytest module form is a supported deterministic runner.
        "python -c 'print(1)'",
        "py -c 'import os'",
        # git mutation subcommands
        "git reset --hard HEAD",
        "git checkout -- .",
        "git clean -fd",
        # empty / garbage
        "",
        "   ",
        "unparseable 'quote",
    ):
        decision = check_verification_command(bad)
        assert not decision.allowed, f"expected reject for {bad!r}, got {decision}"


def case_v002_policy_allows_deterministic() -> None:
    from harness.verification.policy import check_verification_command

    for ok in (
        "pytest -q",
        "pytest tests -q",
        "ruff check .",
        "mypy src",
        "node --test",
        "npm test",
        "python tests/check.py",
        "py scripts/smoke.py",
        "git diff --stat",
        "git status --short",
        "git log --oneline -5",
    ):
        decision = check_verification_command(ok)
        assert decision.allowed, f"expected allow for {ok!r}, got {decision.reason}"


# --- V003 / V004: pass / fail execution --------------------------------------

def case_v003_success_sets_passing() -> None:
    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        _write_script(ws, "smoke_ok.py", "import sys; sys.exit(0)")
        feat = create_feature(
            "smoke",
            "process exits 0",
            "python smoke_ok.py",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)

        assert result.state == "passing", f"state={result.state} err={result.last_error}"
        assert result.completed_at is not None
        assert len(result.evidence) == 1
        ev = result.evidence[0]
        assert ev["exit_code"] == 0
        assert ev["verified_by"] == "runner"
        assert ev["command"].startswith("python smoke_ok.py")

        # Durable: reload from disk.
        assert get_feature(feat.id, workspace=ws).state == "passing"
    finally:
        tmp.cleanup()


def case_v004_failure_sets_failing() -> None:
    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        _write_script(ws, "smoke_fail.py", "import sys; sys.exit(1)")
        feat = create_feature(
            "smoke",
            "process exits 1",
            "python smoke_fail.py",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)

        assert result.state == "failing"
        assert result.attempts == 1
        assert "exit code 1" in (result.last_error or "")
        assert result.evidence[0]["exit_code"] == 1
        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- V005: timeout -----------------------------------------------------------

def case_v005_timeout_kills_and_fails() -> None:
    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        _write_script(ws, "hang.py", "import time; time.sleep(60)")
        feat = create_feature(
            "hang",
            "must not hang forever",
            "python hang.py",
            workspace=ws,
        )
        started = time.monotonic()
        result = verify_feature_command(feat.id, workspace=ws, timeout_s=2)
        elapsed = time.monotonic() - started

        assert result.state == "failing"
        assert "timed out" in (result.last_error or "").lower()
        assert result.evidence[0]["exit_code"] == 124  # timeout convention
        # The process must actually have been killed, not left running.
        assert elapsed < 15, f"timeout path took {elapsed:.1f}s — process not killed?"
        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- V006: permission gate ---------------------------------------------------

def case_v006_permission_deny_blocks_run() -> None:
    """A non-allow permission decision must stop the run before exec."""
    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command
    from harness.verification import runner as ver_runner

    tmp, ws = _tmp_workspace()
    try:
        _write_script(ws, "smoke_ok.py", "import sys; sys.exit(0)")
        feat = create_feature(
            "perm-gated",
            "denied by config",
            "python smoke_ok.py",
            workspace=ws,
        )
        fake_decision = mock.Mock()
        fake_decision.effect = "ask"

        with mock.patch.object(
            ver_runner, "evaluate_single_permission", return_value=fake_decision
        ):
            result = verify_feature_command(feat.id, workspace=ws)

        assert result.state == "failing"
        assert "does not allow" in (result.last_error or "").lower()
        # Never executed -> no evidence attached.
        assert result.evidence == []
        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- V007: output trimming ---------------------------------------------------

def case_v007_output_trimmed() -> None:
    from harness.features import create_feature
    from harness.verification import verify_feature_command
    from harness.verification.evidence import EVIDENCE_TAIL_CHARS

    tmp, ws = _tmp_workspace()
    try:
        _write_script(ws, "noisy.py", "print('x' * 20000)")
        feat = create_feature(
            "noisy",
            "prints a lot",
            "python noisy.py",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)
        assert result.state == "passing"
        tail = result.evidence[0]["stdout_tail"]
        assert len(tail) <= EVIDENCE_TAIL_CHARS + 1
        assert tail.endswith("x" * 100)  # keeps the tail, not the head
    finally:
        tmp.cleanup()


# --- V008: policy rejection path ---------------------------------------------

def case_v008_policy_rejection_marks_failing() -> None:
    """A policy-rejected command never runs and never fabricates evidence."""
    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature(
            "bad-cmd",
            "declared a destructive verification",
            "rm -rf .",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)

        assert result.state == "failing"
        assert "policy rejected" in (result.last_error or "")
        assert result.evidence == []
        # The workspace must still exist — nothing was executed.
        assert ws.exists()
        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- V009: workspace mutation detection --------------------------------------

def case_v009_workspace_mutation_detected() -> None:
    """A verification command that modifies the (git) workspace must fail."""
    import subprocess

    from harness.features import create_feature, get_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        subprocess.run(
            ["git", "init", "-q", str(ws)],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=str(ws), check=False)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=False)
        _write_script(ws, "base.py", "x = 1")
        subprocess.run(["git", "add", "."], cwd=str(ws), check=False)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(ws), check=False)

        _write_script(ws, "mutate.py", "from pathlib import Path; Path('pwned.txt').write_text('x')")
        feat = create_feature(
            "mutator",
            "verification must be read-only",
            "python mutate.py",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)

        assert result.state == "failing"
        assert "modified the workspace" in (result.last_error or "")
        assert result.evidence == []  # mutation => no passing evidence
        assert get_feature(feat.id, workspace=ws).state == "failing"
    finally:
        tmp.cleanup()


# --- V010: script must exist in workspace ------------------------------------

def case_v010_script_must_exist() -> None:
    from harness.features import create_feature
    from harness.verification import verify_feature_command

    tmp, ws = _tmp_workspace()
    try:
        feat = create_feature(
            "missing-script",
            "script must exist",
            "python does_not_exist.py",
            workspace=ws,
        )
        result = verify_feature_command(feat.id, workspace=ws)
        assert result.state == "failing"
        assert "does not exist" in (result.last_error or "")
        assert result.evidence == []
    finally:
        tmp.cleanup()


CASES = [
    EvalCase(
        "v001.policy_rejects_destructive",
        "V001: verification policy rejects destructive/shell/python -c commands while allowing Goal pytest module runs",
        "verification",
        case_v001_policy_rejects_destructive,
    ),
    EvalCase(
        "v002.policy_allows_deterministic",
        "V002: verification policy allows deterministic commands",
        "verification",
        case_v002_policy_allows_deterministic,
    ),
    EvalCase(
        "v003.success_sets_passing",
        "V003: successful verification sets feature passing with evidence",
        "verification",
        case_v003_success_sets_passing,
    ),
    EvalCase(
        "v004.failure_sets_failing",
        "V004: failed verification sets feature failing with last_error",
        "verification",
        case_v004_failure_sets_failing,
    ),
    EvalCase(
        "v005.timeout_kills_process",
        "V005: timeout kills the process and marks failing",
        "verification",
        case_v005_timeout_kills_and_fails,
    ),
    EvalCase(
        "v006.permission_deny_blocks",
        "V006: non-allow permission blocks verification before exec",
        "verification",
        case_v006_permission_deny_blocks_run,
    ),
    EvalCase(
        "v007.output_trimmed",
        "V007: long verification output is trimmed in evidence",
        "verification",
        case_v007_output_trimmed,
    ),
    EvalCase(
        "v008.policy_rejection_failing",
        "V008: policy rejection marks failing without fake evidence",
        "verification",
        case_v008_policy_rejection_marks_failing,
    ),
    EvalCase(
        "v009.workspace_mutation_detected",
        "V009: verification that modifies the workspace is reported failed",
        "verification",
        case_v009_workspace_mutation_detected,
    ),
    EvalCase(
        "v010.script_must_exist",
        "V010: python script must exist inside the workspace",
        "verification",
        case_v010_script_must_exist,
    ),
]
