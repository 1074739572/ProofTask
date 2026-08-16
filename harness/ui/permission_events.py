"""JSONL permission request/reply broker for alternate frontends."""
from __future__ import annotations
import threading
import time
import uuid
from collections.abc import Callable
from harness.settings import PERMISSION_AUTO_APPROVE_TIMEOUT
from harness.ui.events import emit

_lock = threading.Condition()
_pending: dict[str, str | None] = {}


def request_permission(
    tool: str,
    resource: str,
    title: str,
    *,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float | None = None,
) -> str:
    """Wait for an explicit event-UI permission decision.

    An unanswered prompt is never permission.  A configured timeout denies the
    request, while cancellation lets the calling Goal stop without leaving its
    worker blocked behind a stale overlay.
    """
    request_id = uuid.uuid4().hex
    with _lock:
        _pending[request_id] = None
    emit(
        "permission_request",
        id=request_id,
        tool=tool,
        resource=resource,
        title=title,
        choices=["allow", "session", "deny"],
    )
    wait_timeout = PERMISSION_AUTO_APPROVE_TIMEOUT if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + wait_timeout if wait_timeout > 0 else None
    with _lock:
        while _pending.get(request_id) is None:
            if cancel_check is not None and cancel_check():
                _pending.pop(request_id, None)
                emit("permission_cancelled", id=request_id, tool=tool, resource=resource)
                return "cancel"
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _pending.pop(request_id, None)
                    emit("permission_timed_out", id=request_id, tool=tool, resource=resource)
                    return "deny"
                _lock.wait(timeout=min(0.25, remaining))
            else:
                _lock.wait(timeout=0.25)
        return _pending.pop(request_id, "deny") or "deny"


def reply_permission(request_id: str, decision: str) -> bool:
    if decision not in {"allow", "session", "deny"}:
        decision = "deny"
    with _lock:
        if request_id not in _pending:
            return False
        _pending[request_id] = decision
        _lock.notify_all()
        return True
