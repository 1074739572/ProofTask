"""In-memory permission mode state for one interactive harness session.

The checked-in ``config/permissions.json`` file describes the available
permission modes and their static risk metadata.  The mode selected by a user
is deliberately kept separate from that file: it is runtime state, scoped to
the process/session, and is reset when a new session is started.

This module has no filesystem/configuration dependencies.  ``PermissionSession``
instances are independent, which makes the lifecycle explicit and keeps tests
and embedded callers from accidentally sharing a mode.  The small module-level
facade is used by the CLI and hook pipeline for the one active interactive
session in a process.
"""

from __future__ import annotations

from threading import RLock
from typing import Final, Literal, TypeAlias


PermissionMode: TypeAlias = Literal["default", "auto-review", "full-access"]

DEFAULT_PERMISSION_MODE: Final[PermissionMode] = "default"
PERMISSION_MODES: Final[tuple[PermissionMode, ...]] = (
    "default",
    "auto-review",
    "full-access",
)
VALID_PERMISSION_MODES: Final[frozenset[str]] = frozenset(PERMISSION_MODES)


class PermissionSession:
    """Thread-safe, non-persistent permission mode holder.

    A newly-created instance always starts in ``default``.  ``set_mode``
    normalizes surrounding whitespace/case for friendly CLI use, but rejects
    every value outside the three policy modes and leaves the previous mode
    untouched when it rejects a value.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._mode: PermissionMode = DEFAULT_PERMISSION_MODE

    @property
    def mode(self) -> PermissionMode:
        with self._lock:
            return self._mode

    @property
    def current_mode(self) -> PermissionMode:
        """Alias used by callers that prefer an explicit property name."""
        return self.mode

    @property
    def current(self) -> PermissionMode:
        """Short read-only alias for integrations that expose ``current``."""
        return self.mode

    @property
    def available_modes(self) -> tuple[PermissionMode, ...]:
        return PERMISSION_MODES

    def get_mode(self) -> PermissionMode:
        """Return the current mode."""
        return self.mode

    def get_current_mode(self) -> PermissionMode:
        return self.mode

    def set_mode(self, mode: str) -> PermissionMode:
        """Set and return a valid mode, or raise :class:`ValueError`.

        Validation happens before acquiring the state-changing section, so an
        invalid request can never partially update the session.
        """
        normalized = mode.strip().lower() if isinstance(mode, str) else ""
        if normalized not in VALID_PERMISSION_MODES:
            available = ", ".join(PERMISSION_MODES)
            raise ValueError(
                f"Unknown permission mode {mode!r}; available modes: {available}"
            )
        selected = normalized  # type: ignore[assignment]
        with self._lock:
            self._mode = selected
            return self._mode

    def reset(self) -> PermissionMode:
        """Reset this session to the safe default mode and return it."""
        with self._lock:
            self._mode = DEFAULT_PERMISSION_MODE
            return self._mode


# The process has one active interactive frontend, so the CLI/hook facade can
# share one holder.  It remains an ordinary ``PermissionSession`` instance;
# creating another instance never changes this one or inherits its mode.
_ACTIVE_SESSION = PermissionSession()
# Public object alias for lightweight integrations that prefer attribute access
# (``permission_session.mode``).  It points at the stable holder, so reset
# operations preserve references held by callers/tests.
permission_session = _ACTIVE_SESSION
session_state = _ACTIVE_SESSION


def get_permission_session() -> PermissionSession:
    """Return the active process session's in-memory permission holder."""
    return _ACTIVE_SESSION


def new_permission_session() -> PermissionSession:
    """Create an independent fresh holder (always starting at ``default``)."""
    return PermissionSession()


def create_permission_session() -> PermissionSession:
    """Compatibility alias for :func:`new_permission_session`."""
    return new_permission_session()


def current_permission_session() -> PermissionSession:
    """Compatibility alias for :func:`get_permission_session`."""
    return get_permission_session()


def get_permission_mode() -> PermissionMode:
    return _ACTIVE_SESSION.get_mode()


def set_permission_mode(mode: str) -> PermissionMode:
    return _ACTIVE_SESSION.set_mode(mode)


def reset_permission_session() -> PermissionSession:
    """Reset the active session without touching any configuration file."""
    _ACTIVE_SESSION.reset()
    return _ACTIVE_SESSION


def reset_session() -> PermissionSession:
    """Short alias used by session lifecycle integrations."""
    return reset_permission_session()


def get_mode() -> PermissionMode:
    """Convenience facade for the active session."""
    return get_permission_mode()


def set_mode(mode: str) -> PermissionMode:
    """Convenience facade for the active session."""
    return set_permission_mode(mode)


# A concise alias is useful to embedders and keeps the state holder discoverable
# without exposing mutable module globals.
PermissionSessionState = PermissionSession
SessionPermissionState = PermissionSession
PermissionModeState = PermissionSession

AVAILABLE_PERMISSION_MODES: Final[tuple[PermissionMode, ...]] = PERMISSION_MODES


__all__ = [
    "DEFAULT_PERMISSION_MODE",
    "AVAILABLE_PERMISSION_MODES",
    "PERMISSION_MODES",
    "VALID_PERMISSION_MODES",
    "PermissionMode",
    "PermissionSession",
    "PermissionSessionState",
    "SessionPermissionState",
    "PermissionModeState",
    "permission_session",
    "session_state",
    "current_permission_session",
    "create_permission_session",
    "get_permission_mode",
    "get_permission_session",
    "get_mode",
    "reset_permission_session",
    "reset_session",
    "new_permission_session",
    "set_permission_mode",
    "set_mode",
]
