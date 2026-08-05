"""Shared CLI terminal flags (no renderer/console imports)."""

from __future__ import annotations

CLI_ACTIVE = False

_READLINE_CACHE: bool | None = None


def readline_available() -> bool:
    """True when the readline module can be imported.

    Lazily probed: on Windows this pulls in pyreadline3 (~0.3s), which the
    TUI/event-stream mode never needs. The check is deferred until something
    actually reads interactive input.
    """
    global _READLINE_CACHE
    if _READLINE_CACHE is None:
        try:
            import readline

            readline.parse_and_bind("set bind-tty-special-chars off")
            _READLINE_CACHE = True
        except ImportError:
            _READLINE_CACHE = False
    return _READLINE_CACHE
