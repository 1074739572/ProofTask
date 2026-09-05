"""Cancellation behavior around blocking provider requests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def _profile():
    return SimpleNamespace(id="m", provider="p", api_model="m", thinking=False)


def _response():
    return SimpleNamespace(content=[], stop_reason="end_turn", model="m", usage=None)


def test_esc_cancels_while_provider_request_is_still_blocked():
    from harness import llm
    from harness.agent.cancel import clear_cancel, request_cancel

    started = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def blocked_provider(**_kwargs):
        started.set()
        release.wait(5)
        return _response()

    def run():
        try:
            llm.create_message(messages=[], max_tokens=10)
        except BaseException as exc:  # cancellation is intentionally surfaced
            outcome["error"] = exc

    clear_cancel()
    try:
        with (
            patch("harness.llm.get_model_profile", return_value=_profile()),
            patch("harness.llm.create_provider_message", side_effect=blocked_provider),
        ):
            thread = threading.Thread(target=run)
            thread.start()
            assert started.wait(1)
            request_cancel()
            thread.join(1)
            assert not thread.is_alive(), "Esc should not wait for the provider timeout"
            assert isinstance(outcome.get("error"), RuntimeError)
            assert "cancel" in str(outcome["error"]).lower()
    finally:
        # Let the daemon provider worker finish before the test process moves
        # on, and reset the process-global flag for the following tests.
        release.set()
        time.sleep(0.05)
        clear_cancel()
