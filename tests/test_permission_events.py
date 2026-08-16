"""Event-stream permission prompts must fail closed and remain cancellable."""

from __future__ import annotations

from harness.ui.permission_events import request_permission


def test_event_permission_timeout_denies_without_user_response():
    assert request_permission("write_file", "config.json", "Allow write?", timeout=0.01) == "deny"


def test_event_permission_cancellation_unblocks_waiter():
    assert request_permission(
        "write_file",
        "config.json",
        "Allow write?",
        cancel_check=lambda: True,
    ) == "cancel"
