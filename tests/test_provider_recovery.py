import os

from harness.agent import recovery
from harness.agent.recovery import RecoveryState, with_retry
from harness.providers.config import provider_timeout


def test_provider_timeout_defaults(monkeypatch):
    for name in (
        "HARNESS_LLM_CONNECT_TIMEOUT",
        "HARNESS_LLM_READ_TIMEOUT",
        "HARNESS_LLM_WRITE_TIMEOUT",
        "HARNESS_LLM_POOL_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert provider_timeout() == (10.0, 90.0, 30.0, 10.0)


def test_provider_timeout_env(monkeypatch):
    monkeypatch.setenv("HARNESS_LLM_CONNECT_TIMEOUT", "3")
    monkeypatch.setenv("HARNESS_LLM_READ_TIMEOUT", "45")
    monkeypatch.setenv("HARNESS_LLM_WRITE_TIMEOUT", "12")
    monkeypatch.setenv("HARNESS_LLM_POOL_TIMEOUT", "4")
    assert provider_timeout() == (3.0, 45.0, 12.0, 4.0)


def test_connection_error_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(recovery, "retry_delay", lambda attempt: 0)
    calls = 0

    def request():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("temporary connection failure")
        return "ok"

    assert with_retry(request, RecoveryState()) == "ok"
    assert calls == 3
