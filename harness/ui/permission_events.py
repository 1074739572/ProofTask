"""JSONL permission request/reply broker for alternate frontends."""
from __future__ import annotations
import threading
import time
import uuid
from harness.settings import PERMISSION_AUTO_APPROVE_TIMEOUT
from harness.ui.events import emit

_lock = threading.Condition()
_pending: dict[str, str | None] = {}


def request_permission(tool: str, resource: str, title: str) -> str:
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
    if PERMISSION_AUTO_APPROVE_TIMEOUT > 0:
        deadline = time.monotonic() + PERMISSION_AUTO_APPROVE_TIMEOUT
        with _lock:
            while _pending.get(request_id) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Nobody answered in time: approve once (never remembered),
                    # which is the safe default for the common "yes, just do it"
                    # case. Users can still deny explicitly before the timeout.
                    _pending.pop(request_id, None)
                    emit(
                        "permission_auto_approved",
                        id=request_id,
                        tool=tool,
                        resource=resource,
                    )
                    return "allow"
                _lock.wait(timeout=min(0.25, remaining))
            decision = _pending.pop(request_id, "deny") or "deny"
    else:
        with _lock:
            while _pending.get(request_id) is None:
                _lock.wait(timeout=0.25)
            decision = _pending.pop(request_id, "deny") or "deny"
    return decision


def reply_permission(request_id: str, decision: str) -> bool:
    if decision not in {"allow", "session", "deny"}:
        decision = "deny"
    with _lock:
        if request_id not in _pending:
            return False
        _pending[request_id] = decision
        _lock.notify_all()
        return True
