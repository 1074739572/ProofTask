"""Classify bash commands by execution transparency.

``opaque``: the command hands control to an interpreter or script whose side
effects cannot be reviewed from the command line. Broad wildcard approvals
(config or saved) must not cover it; only an exact command-level authorization
satisfies it.

``action_time``: irreversible local deletion. Every occurrence requires its
own confirmation — saved approvals, broad config allows, and session
convenience modes (including full-access) never substitute for it.
"""

from __future__ import annotations

import re
from typing import Literal

ExecutionClass = Literal["normal", "opaque", "action_time"]

_DELETE_RE = re.compile(
    r"^\s*(?:del|erase|rd|rmdir)(?:[\s/]|$)|^\s*Remove-Item(?:\s|$)",
    re.IGNORECASE,
)

_INLINE_CODE_RE = re.compile(r"(?:^|\s)(?:-c|-e|--eval|-p)(?:\s|$)")
_POWERSHELL_CODE_RE = re.compile(
    r"(?:^|\s)-(?:file|command|c|enc|encodedcommand)\b", re.IGNORECASE
)
_CMD_CALL_RE = re.compile(r"(?:^|\s)/[ck](?:\s|$)", re.IGNORECASE)

_PYTHON_SCRIPT_EXTS = (".py",)
_NODE_SCRIPT_EXTS = (".js", ".mjs", ".cjs", ".ts")
_POWERSHELL_SCRIPT_EXTS = (".ps1",)
_SHELL_SCRIPT_EXTS = (".sh",)
_DIRECT_SCRIPT_EXTS = (
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
    ".py",
    ".js",
    ".mjs",
    ".exe",
    ".com",
    ".msi",
)


def _split_invocation(command: str) -> tuple[str, str]:
    parts = command.strip().split(None, 1)
    if not parts:
        return "", ""
    token = parts[0].strip("\"'").lower()
    rest = parts[1] if len(parts) > 1 else ""
    return token, rest


def _has_script_arg(rest: str, exts: tuple[str, ...]) -> bool:
    for arg in rest.split():
        if arg.strip("\"'").lower().endswith(exts):
            return True
    return False


def _is_opaque(command: str) -> bool:
    token, rest = _split_invocation(command)
    if not token:
        return False
    base = token.replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(_DIRECT_SCRIPT_EXTS):
        return True
    if base in ("python", "python3", "py") or re.fullmatch(r"python\d+(\.\d+)?", base):
        if bool(_INLINE_CODE_RE.search(rest)):
            return True
        # `python -m <module>` invokes a named tool (pytest, pip, venv): the
        # module is the reviewed entry point, so ordinary config prefixes such
        # as `python -m pytest*` stay meaningful. Trailing .py arguments are
        # data for the module, not the executed script.
        if re.search(r"(?:^|\s)-m(?:\s|$)", rest):
            return False
        return _has_script_arg(rest, _PYTHON_SCRIPT_EXTS)
    if base in ("node", "nodejs"):
        return bool(_INLINE_CODE_RE.search(rest)) or _has_script_arg(
            rest, _NODE_SCRIPT_EXTS
        )
    if base in ("powershell", "pwsh"):
        return bool(_POWERSHELL_CODE_RE.search(rest)) or _has_script_arg(
            rest, _POWERSHELL_SCRIPT_EXTS
        )
    if base == "cmd":
        return bool(_CMD_CALL_RE.search(rest))
    if base in ("bash", "sh"):
        return bool(_INLINE_CODE_RE.search(rest)) or _has_script_arg(
            rest, _SHELL_SCRIPT_EXTS
        )
    return False


def classify_bash_execution(command: str) -> ExecutionClass:
    """Classify a single shell command (no separators) by transparency.

    Classification only ever makes decisions stricter, never more permissive:
    an unrecognized command stays ``normal`` and follows the ordinary
    rule/risk path.
    """
    cmd = (command or "").strip()
    if not cmd:
        return "normal"
    if _DELETE_RE.search(cmd):
        return "action_time"
    if _is_opaque(cmd):
        return "opaque"
    return "normal"


def has_wildcard(pattern: str) -> bool:
    """Return whether a rule pattern is broader than an exact command string."""
    return any(ch in pattern for ch in "*?[")
