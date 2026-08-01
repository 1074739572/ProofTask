"""JSONL permission request/reply broker for alternate frontends."""
from __future__ import annotations

import threading
import uuid

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
