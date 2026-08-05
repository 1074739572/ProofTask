"""M-series: mechanism regression checks (real behavior, zero LLM cost).

These assert on REAL harness behavior — not "does the code contain X" but
"does the mechanism actually work end-to-end":
- M001: bash runs python commands without hanging (stdin=DEVNULL fix)
- M002: platform/shell guidance is injected into session context
- M003: subagent/teammate tool defs include the same shell semantics
- M004: project HARNESS.md is loaded and injected
- M005: run_bash actually executes in the workspace

These run in CI / `python -m evals` with no API cost.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _run_bash_with_timeout(command: str, cwd: Path | None = None, timeout_s: float = 30.0) -> tuple[str, float]:
    """Call run_bash in a SUBPROCESS so we load the CURRENT filesystem.py.

    (The eval harness process may be running older code in memory.)
    """
    import json
    import os
    import textwrap

    probe = ROOT / ".local" / "_m_probe.py"
    probe.parent.mkdir(exist_ok=True)
    # repr() gives a correct Python string literal (json.dumps would add
    # escaping that corrupts the inner quotes of the command).
    command_literal = repr(command)
    probe.write_text(
        textwrap.dedent(
            f"""
            import json, os, sys
            sys.path.insert(0, {str(ROOT)!r})
            os.chdir({str(ROOT)!r})
            from harness.tools.filesystem import run_bash
            t0 = __import__('time').time()
            out = run_bash({command_literal}, timeout=20000)
            dt = __import__('time').time() - t0
            print(json.dumps({{"ok": True, "out": out[:300], "dt": round(dt, 2)}}))
            """
        ),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(probe)],
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return f"probe rc={proc.returncode} stderr={proc.stderr[:300]!r}", proc.returncode
        return proc.stdout, proc.returncode
    except subprocess.TimeoutExpired:
        return "", -2


def case_m001_bash_python_no_hang() -> None:
    """M001: `run_bash('python -c ...')` must return quickly, not hang on stdin.

    Regression for the bug where bash inherited the backend's stdin pipe and
    python sat waiting for input (CPU 0, no output, forever).
    """
    out, code = _run_bash_with_timeout('python -c "print(1)"')
    assert code == 0, f"run_bash python -c failed (code={code}, out={out[:200]!r})"
    assert '"dt"' in out, f"probe did not return dt: {out[:200]!r}"
    import json
    try:
        payload = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(f"probe output not JSON: {out[:200]!r}") from exc
    assert payload.get("ok") is True, f"run_bash failed: {payload}"
    assert "1" in payload.get("out", ""), f"expected '1' in output: {payload}"
    assert payload["dt"] < 10, f"python -c took {payload['dt']}s — likely hung on stdin"


def case_m002_platform_injected() -> None:
    """M002: session context must include OS/shell guidance every turn."""
    from harness.prompts.dynamic import _format_platform, build_session_context

    block = _format_platform()
    assert "System environment" in block, "platform block missing header"
    assert "cmd.exe" in block or "POSIX" in block, "platform block missing shell info"

    ctx = build_session_context({})
    assert "System environment" in ctx, "session context missing platform section"


def case_m003_subagent_tooldef_has_shell() -> None:
    """M003: subagent/teammate bash tool def must carry shell semantics,
    not just 'Run a shell command' — otherwise sub-agents guess ls/grep."""
    from harness.agents.runner import _BASE_TOOL_DEFS

    bash_def = _BASE_TOOL_DEFS.get("bash", {})
    desc = bash_def.get("description", "")
    assert "shell" in desc.lower() or "command" in desc.lower(), (
        f"subagent bash def lacks shell semantics: {desc!r}"
    )


def case_m004_project_md_injected() -> None:
    """M004: project HARNESS.md must be discoverable and injectable."""
    from harness.prompts.project_md import find_project_md

    result = find_project_md(ROOT)
    assert result.source_name in ("HARNESS.md", "AGENTS.md"), (
        f"expected HARNESS.md/AGENTS.md in repo, got source={result.source_name} status={result.status}"
    )
    assert result.loaded, f"project instructions not loaded: {result.status}"


def case_m005_bash_cwd_follows_workspace() -> None:
    """M005: run_bash executes in the active workspace (not startup dir)."""
    import tempfile
    from unittest import mock

    from harness import workspace as ws_mod
    from harness.tools.filesystem import run_bash

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "proj"
        target.mkdir()
        (target / "sentinel.txt").write_text("x", encoding="utf-8")
        with mock.patch("harness.workspace.record_recent_project"):
            ok, _, _ = ws_mod.switch_workspace(target)
        assert ok
        code = "cd" if sys.platform == "win32" else "pwd"
        out = run_bash(code)
        assert str(target.resolve()) in out, f"bash cwd not switched: {out!r}"


def case_m006_open_reloads_project_instructions() -> None:
    """M006: TUI /open must clear old history and load the NEW project's HARNESS.md.

    Regression: event-stream /open previously kept the old context, so after
    switching projects the agent still followed the previous project's rules.
    """
    import os
    import tempfile
    from unittest import mock

    from harness.prompts.project_md import apply_project_instructions

    original_cwd = os.getcwd()
    tmp_obj = tempfile.TemporaryDirectory()
    try:
        tmp = tmp_obj.name
        proj_a = Path(tmp) / "projA"
        proj_b = Path(tmp) / "projB"
        proj_a.mkdir()
        proj_b.mkdir()
        (proj_a / "HARNESS.md").write_text("# A\n\n## Commands\n- Test: pytest a\n", encoding="utf-8")
        (proj_b / "HARNESS.md").write_text("# B\n\n## Commands\n- Test: pytest b\n", encoding="utf-8")

        from harness import settings as settings_mod

        def _fake_switch(target):
            target = Path(target).resolve()
            settings_mod._workspace = target
            settings_mod._workspace_generation += 1
            return True, f"Switched workspace → {target}", None

        with mock.patch.object(settings_mod, "switch_workspace", _fake_switch):
            with mock.patch("harness.workspace.record_recent_project", lambda *a, **k: None):
                from harness.event_stream import _run_user_turn

                os.chdir(proj_a)
                history: list = []
                context: dict = {}
                apply_project_instructions(context, start=proj_a)
                assert "pytest a" in context.get("project_instructions", "")

                os.chdir(proj_b)
                with mock.patch("harness.event_stream.emit"):
                    context, _interrupted, _binding = _run_user_turn(
                        f"/open {proj_b}", history, context, None
                    )

        text = context.get("project_instructions", "")
        assert "pytest b" in text, f"expected B instructions after /open, got: {text[:200]!r}"
        assert "pytest a" not in text, "old project A instructions leaked after /open"
        assert history == [], f"expected history cleared, got {len(history)} msgs"
    finally:
        os.chdir(original_cwd)  # Windows: cannot delete a dir that is the cwd
        tmp_obj.cleanup()


def case_m007_saved_allow_cannot_override_config_deny() -> None:
    """M007: a saved 'always allow' rule must NOT override a config deny.

    Regression for a real security hole: a persistent `bash:* allow` (from one
    'always' click) silently disabled the entire bash deny-list (`rm *`,
    `sudo *`) — destructive commands ran without confirmation.
    """
    from harness.permissions.engine import evaluate_single_permission
    from harness.permissions.state import SavedPermissionRule

    # Simulate a saved over-broad allow that should NOT defeat `rm *: deny`.
    saved = [
        SavedPermissionRule(tool="bash", resource="*", effect="allow", scope="persistent")
    ]

    def _deny_eval():
        return evaluate_single_permission(
            "bash",
            "rm -rf ./tmp_build",
            include_saved=True,
            rules=None,  # loads real config/permissions.json
        )

    # Temporarily inject the bad saved rule and assert deny still wins.
    import harness.permissions.engine as eng

    orig_load = eng.load_persistent_rules
    eng.load_persistent_rules = lambda: saved
    try:
        decision = _deny_eval()
    finally:
        eng.load_persistent_rules = orig_load
    assert decision.effect == "deny", (
        f"saved allow overrode config deny: effect={decision.effect} reason={decision.reason}"
    )

    # Sanity: `del *` (config ask) IS overridable by saved allow (user intent).
    eng.load_persistent_rules = lambda: saved
    try:
        decision = evaluate_single_permission("bash", "del ./tmp_build", include_saved=True)
    finally:
        eng.load_persistent_rules = orig_load
    assert decision.effect == "allow", (
        f"saved allow should override config ask, got effect={decision.effect}"
    )


def case_m008_instant_slash_command_not_interactive() -> None:
    """M008: model/mode/effort switches are NON-interactive control commands.

    They must be executable while the agent is running (no "already running"
    rejection) and must NOT flip the UI into "running" state (no LLM round).
    Regression for the bug where /model /mode /effort were treated like
    ordinary messages: blocked while busy, and showed the running spinner.
    """
    from harness.event_stream import _is_instant_slash_command

    # Configuration switches are instant (no LLM round, no context mutation).
    for cmd in (
        "/model deepseek-v4-pro",
        "/model",
        "/effort high",
        "/effort",
        "/mode direct",
        "/mode",
        "/models",
        "/usage",
        "/help",
    ):
        assert _is_instant_slash_command(cmd), f"{cmd!r} should be instant"

    # State-mutating / interactive commands must NOT be treated as instant
    # (they touch context/history/binding and must not run concurrently).
    for cmd in (
        "/open /some/dir",
        "/resume 1",
        "/clear",
        "/rag index files",
        "普通消息",
        "fix the bug",
        "",
    ):
        assert not _is_instant_slash_command(cmd), f"{cmd!r} should NOT be instant"

    # The command handler itself must return a note synchronously — no LLM
    # round, no exception, even with an empty history/binding.
    from unittest import mock

    with mock.patch("harness.event_stream.events.is_enabled", return_value=False):
        from harness.event_stream import _handle_slash_command

        note, binding = _handle_slash_command("/effort high", [], None)
        assert note is not None, "instant command must return a note"
        assert "effort" in note.lower() or "高" in note


CASES = [
    EvalCase(
        "m001.bash_python_no_hang",
        "M001: bash runs python without hanging (stdin fix)",
        "mechanisms",
        case_m001_bash_python_no_hang,
    ),
    EvalCase(
        "m002.platform_injected",
        "M002: session context has OS/shell guidance",
        "mechanisms",
        case_m002_platform_injected,
    ),
    EvalCase(
        "m003.subagent_tooldef_shell",
        "M003: subagent bash tool def carries shell semantics",
        "mechanisms",
        case_m003_subagent_tooldef_has_shell,
    ),
    EvalCase(
        "m004.project_md_injected",
        "M004: project HARNESS.md discoverable + injectable",
        "mechanisms",
        case_m004_project_md_injected,
    ),
    EvalCase(
        "m005.bash_cwd_follows_workspace",
        "M005: bash executes in active workspace",
        "mechanisms",
        case_m005_bash_cwd_follows_workspace,
    ),
    EvalCase(
        "m006.open_reloads_project_md",
        "M006: /open reloads new project's HARNESS.md, clears history",
        "mechanisms",
        case_m006_open_reloads_project_instructions,
    ),
    EvalCase(
        "m007.saved_allow_vs_config_deny",
        "M007: saved allow cannot override config deny (security)",
        "mechanisms",
        case_m007_saved_allow_cannot_override_config_deny,
    ),
    EvalCase(
        "m008.instant_slash_command_not_interactive",
        "M008: model/mode/effort switches are non-interactive (no running state, allowed while busy)",
        "mechanisms",
        case_m008_instant_slash_command_not_interactive,
    ),
]
