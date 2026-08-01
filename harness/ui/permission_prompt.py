"""Cancel-aware permission prompts for classic CLI."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from harness.agent.cancel import is_cancelled, request_cancel
from harness.ui.interrupt_listener import pause_key_poll, resume_key_poll


@dataclass(frozen=True)
class PermissionResponse:
    request_id: str
    decision: str
    value: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "session", "always")

    @property
    def remember_session(self) -> bool:
        return self.decision == "session"

    @property
    def remember_always(self) -> bool:
        return self.decision == "always"


def ask_permission(
    prompt: str = "  Allow? [y/N] ",
    *,
    detail: str | None = None,
    title: str | None = None,
    editable: bool = False,
    remember: bool = False,
) -> PermissionResponse:
    """Return a structured decision and an optionally edited value.

    Decisions:
      allow   — approve once
      session — approve and remember for this process
      always  — approve and persist for future runs
      deny    — reject
      cancel  — Esc/Ctrl+C or cooperative cancellation
    """
    body = (detail if detail is not None else prompt).strip()
    if remember:
        prompt = "  Allow? [y] once / [s] session / [a] always / [N] deny "
        choice = ask_choice(prompt, allowed={"y", "s", "a", "n", "", "\r", "\n"})
        if choice is None:
            decision = "cancel"
        elif choice == "y":
            decision = "allow"
        elif choice == "s":
            decision = "session"
        elif choice == "a":
            decision = "always"
        else:
            decision = "deny"
        return PermissionResponse("classic-permission", decision, body)

    choice = ask_allow(prompt, detail=detail, title=title)
    decision = "cancel" if choice is None else ("allow" if choice else "deny")
    return PermissionResponse("classic-permission", decision, body)


def ask_allow(
    prompt: str = "  Allow? [y/N] ",
    *,
    detail: str | None = None,
    title: str | None = None,
) -> bool | None:
    """Ask y/N without blocking forever under Esc/Ctrl+C."""
    choice = ask_choice(prompt, allowed={"y", "n", "", "\r", "\n"})
    if choice is None:
        return None
    return choice == "y"


def ask_choice(prompt: str, *, allowed: set[str]) -> str | None:
    print(prompt, end="", flush=True)
    pause_key_poll()
    try:
        if sys.stdin.isatty() and sys.platform == "win32":
            return _ask_windows_choice(allowed)
        if sys.stdin.isatty():
            return _ask_unix_choice(allowed)
        if is_cancelled():
            print()
            return None
        line = sys.stdin.readline()
        if is_cancelled():
            print()
            return None
        choice = (line or "").strip().lower()
        return choice if choice in allowed else ""
    finally:
        resume_key_poll()


def _ask_windows_choice(allowed: set[str]) -> str | None:
    import msvcrt

    while True:
        if is_cancelled():
            print()
            return None
        if not msvcrt.kbhit():
            time.sleep(0.04)
            continue
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            if msvcrt.kbhit():
                msvcrt.getch()
            continue
        if ch in (b"\x03", b"\x1b"):
            request_cancel()
            print()
            return None
        if ch in (b"\r", b"\n"):
            print()
            return ""
        try:
            choice = ch.decode("utf-8", errors="ignore").lower()
        except Exception:
            choice = ""
        if choice in allowed:
            print(choice or "")
            return choice


def _ask_unix_choice(allowed: set[str]) -> str | None:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            if is_cancelled():
                print()
                return None
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready:
                continue
            ch = sys.stdin.read(1)
            if ch in ("\x03", "\x1b"):
                request_cancel()
                print()
                return None
            if ch in ("\r", "\n"):
                print()
                return ""
            choice = ch.lower()
            if choice in allowed:
                print(choice or "")
                return choice
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
