"""Cancel-aware permission prompts for classic CLI."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from harness.agent.cancel import is_cancelled, request_cancel
from harness.settings import PERMISSION_AUTO_APPROVE_TIMEOUT
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


def _deadline_exceeded(start: float, timeout: float | None) -> bool:
    return timeout is not None and timeout > 0 and (time.monotonic() - start) >= timeout


def ask_permission(
    prompt: str = "  Allow? [y/N] ",
    *,
    detail: str | None = None,
    title: str | None = None,
    editable: bool = False,
    remember: bool = False,
    timeout: float | None = None,
) -> PermissionResponse:
    """Return a structured decision and an optionally edited value.

    Decisions:
      allow   — approve once
      session — approve and remember for this process
      always  — approve and persist for future runs
      deny    — reject
      cancel  — Esc/Ctrl+C or cooperative cancellation

    If ``timeout`` seconds elapse without a human reply, the prompt is
    approved once (``allow``) so long-running turns are not stuck waiting.
    ``None``/0 keeps the old wait-forever behavior.
    """
    if timeout is None:
        timeout = PERMISSION_AUTO_APPROVE_TIMEOUT
    body = (detail if detail is not None else prompt).strip()
    if remember:
        prompt = "  Allow? [y] once / [s] session / [a] always / [N] deny "
        choice = ask_choice(prompt, allowed={"y", "s", "a", "n", "", "\r", "\n"}, timeout=timeout)
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

    choice = ask_allow(prompt, detail=detail, title=title, timeout=timeout)
    decision = "cancel" if choice is None else ("allow" if choice else "deny")
    return PermissionResponse("classic-permission", decision, body)


def ask_allow(
    prompt: str = "  Allow? [y/N] ",
    *,
    detail: str | None = None,
    title: str | None = None,
    timeout: float | None = None,
) -> bool | None:
    """Ask y/N without blocking forever under Esc/Ctrl+C."""
    choice = ask_choice(prompt, allowed={"y", "n", "", "\r", "\n"}, timeout=timeout)
    if choice is None:
        return None
    return choice == "y"


def ask_choice(prompt: str, *, allowed: set[str], timeout: float | None = None) -> str | None:
    print(prompt, end="", flush=True)
    pause_key_poll()
    start = time.monotonic()
    try:
        if sys.stdin.isatty() and sys.platform == "win32":
            choice = _ask_windows_choice(allowed, start, timeout)
        elif sys.stdin.isatty():
            choice = _ask_unix_choice(allowed, start, timeout)
        else:
            choice = _ask_piped_choice(allowed, start, timeout)
        if choice is None and _deadline_exceeded(start, timeout):
            print("[no reply in time — auto-approved]")
            return "y"
        return choice
    finally:
        resume_key_poll()


def _ask_windows_choice(allowed: set[str], start: float, timeout: float | None) -> str | None:
    import msvcrt

    while True:
        if is_cancelled():
            print()
            return None
        if _deadline_exceeded(start, timeout):
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


def _ask_unix_choice(allowed: set[str], start: float, timeout: float | None) -> str | None:
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
            if _deadline_exceeded(start, timeout):
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


def _ask_piped_choice(allowed: set[str], start: float, timeout: float | None) -> str | None:
    """Non-tty stdin: read a line, but do not hang forever on a silent pipe."""
    if is_cancelled():
        print()
        return None
    line = _readline_with_deadline(start, timeout)
    if line is None:
        return None
    if is_cancelled():
        print()
        return None
    choice = (line or "").strip().lower()
    return choice if choice in allowed else ""


def _readline_with_deadline(start: float, timeout: float | None) -> str | None:
    if timeout is None or timeout <= 0:
        return sys.stdin.readline()
    result: list[str | None] = [None]

    def _read() -> None:
        result[0] = sys.stdin.readline()

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    remaining = timeout - (time.monotonic() - start)
    reader.join(max(0.0, remaining))
    if reader.is_alive():
        return None
    return result[0]
