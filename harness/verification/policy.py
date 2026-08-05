"""Verification command policy (L3) — what may run as a verification command.

A verification command is NOT arbitrary agent shell. It must be:

- deterministic and read-only in effect (no interaction, no file mutations,
  no network side effects);
- declared by the feature itself (``feature.verification``), not invented
  at call time;
- executed in a controlled way (structured argv, fixed cwd, closed stdin,
  hard timeout, bounded output — see :mod:`harness.verification.runner`).

Policy gate (this module, structural):

- the leading program must be in the allowlist;
- shell metacharacters (``&&`` / ``;`` / ``|`` / redirection / command
  substitution) are rejected anywhere — verification is never run through
  a shell;
- ``python -c`` / ``py -c`` (arbitrary inline code) is rejected; ``python``
  is only allowed to run a repository script (``python path/to/script.py``),
  and the runner additionally verifies the script exists inside the workspace;
- ``git`` is restricted to read-only subcommands;
- destructive tokens anywhere reject the command.

Permission gate (existing engine, security red line): the runner requires an
explicit ``allow`` decision on the dedicated ``verify_command`` permission
domain — ``ask`` and ``deny`` both refuse to execute. Saved "always allow"
rules are ignored; the policy allowlist is the only positive grant.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

#: Leading programs allowed as verification commands.
ALLOWED_PROGRAMS = frozenset(
    {
        "pytest",
        "python",
        "py",
        "ruff",
        "mypy",
        "node",
        "npm",
        "npx",
        "go",
        "cargo",
        "dotnet",
        "mvn",
        "gradle",
        "git",
    }
)

#: `git` is only allowed for read-only inspection subcommands.
GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "diff",
        "status",
        "show",
        "log",
        "ls-files",
        "check-ignore",
        "blame",
        "rev-parse",
        "describe",
        "branch",
        "tag",
    }
)

#: Tokens that reject the command anywhere it appears (destructive /
#: system-mutating / output-redirecting).
DENY_TOKENS = frozenset(
    {
        "rm",
        "rmdir",
        "del",
        "erase",
        "remove",
        "mv",
        "sudo",
        "reboot",
        "shutdown",
        "poweroff",
        "halt",
        "format",
        "mkfs",
        "dd",
        "chmod",
        "chown",
        "curl",
        "wget",
    }
)

#: Shell metacharacters / operators that reject the command (no shell allowed).
SHELL_METACHARACTERS = frozenset(
    {
        "&&",
        "||",
        ";",
        "|",
        "&",
        ">",
        ">>",
        "<",
        "2>",
        "2>>",
        "2>&1",
        "`",
        "$(",
        "${",
    }
)

MAX_VERIFICATION_COMMAND_LEN = 1000


@dataclass(frozen=True)
class VerificationDecision:
    allowed: bool
    reason: str = ""


def _leading_program(tokens: list[str]) -> str:
    if not tokens:
        return ""
    prog = tokens[0]
    # Windows may invoke `python.exe` / `py.exe` — normalize the extension.
    if prog.lower().endswith(".exe"):
        prog = prog[:-4]
    return prog.lower()


def check_verification_command(command: str) -> VerificationDecision:
    """Structural gate: is this string acceptable as a verification command?"""
    stripped = (command or "").strip()
    if not stripped:
        return VerificationDecision(False, "empty verification command")
    if len(stripped) > MAX_VERIFICATION_COMMAND_LEN:
        return VerificationDecision(
            False, f"command too long ({len(stripped)} > {MAX_VERIFICATION_COMMAND_LEN})"
        )
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        return VerificationDecision(False, f"unparseable command: {exc}")
    if not tokens:
        return VerificationDecision(False, "empty verification command")

    # No shell operators / redirection / substitution anywhere.
    for token in tokens:
        for meta in SHELL_METACHARACTERS:
            if meta in token:
                return VerificationDecision(
                    False, f"shell metacharacter {meta!r} not allowed in verification command"
                )

    prog = _leading_program(tokens)
    if prog not in ALLOWED_PROGRAMS:
        return VerificationDecision(
            False,
            f"program {tokens[0]!r} not in verification allowlist "
            f"({sorted(ALLOWED_PROGRAMS)})",
        )

    if prog == "git":
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        if sub not in GIT_READONLY_SUBCOMMANDS:
            return VerificationDecision(
                False,
                f"git subcommand {sub!r} not allowed for verification "
                f"(read-only: {sorted(GIT_READONLY_SUBCOMMANDS)})",
            )

    # `python -c` embeds arbitrary code — reject. python may only run a
    # repository script (runner validates the script path is inside workspace).
    if prog in ("python", "py"):
        if len(tokens) > 1 and tokens[1] in ("-c", "-m", "-i", "-I"):
            return VerificationDecision(
                False, f"python flag {tokens[1]!r} not allowed; run a repository script instead"
            )

    for token in tokens:
        if token.lower() in DENY_TOKENS:
            return VerificationDecision(
                False, f"destructive token {token!r} not allowed in verification command"
            )

    return VerificationDecision(True, "ok")
