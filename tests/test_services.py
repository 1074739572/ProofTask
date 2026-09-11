"""Tests for Layer 4 durable service management and its tool integration."""

import sys
import time

import pytest


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    """Redirect the service registry into a temp dir so tests stay hermetic."""
    from harness.agent import services

    monkeypatch.setattr(services, "SERVICES_DIR", tmp_path / "services")
    services.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    return services


def _dummy_command(seconds: int = 60) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_list_services_does_not_deadlock(isolated_registry):
    """list_services() re-enters the registry lock; it must not block forever."""
    services = isolated_registry
    services.register_service(
        "svc_a", command=_dummy_command(), cwd=None, description="alpha"
    )

    started = time.time()
    result = services.list_services()
    elapsed = time.time() - started

    assert elapsed < 5, "list_services() appears to have deadlocked"
    assert [item["service_id"] for item in result] == ["svc_a"]


def test_unregister_force_does_not_deadlock(isolated_registry):
    """unregister_service(force=True) calls stop_service while holding the lock."""
    services = isolated_registry
    services.register_service(
        "svc_b", command=_dummy_command(), cwd=None, description="beta"
    )

    started = time.time()
    message = services.unregister_service("svc_b", force=True)
    elapsed = time.time() - started

    assert elapsed < 10, "unregister_service(force=True) appears to have deadlocked"
    assert "unregistered" in message


def test_service_lifecycle_register_start_status_stop(isolated_registry):
    """Full lifecycle against a real detached process."""
    services = isolated_registry
    services.register_service(
        "svc_life",
        command=_dummy_command(60),
        cwd=None,
        description="lifecycle probe",
    )

    status = services.get_service_status("svc_life")
    assert status["is_running"] is False
    assert status["pid"] is None
    assert status["cwd"] is None
    assert status["health_url"] is None

    start_message = services.start_service("svc_life", health_probe_retries=1)
    assert "started" in start_message

    status = services.get_service_status("svc_life")
    assert status["is_running"] is True
    assert isinstance(status["pid"], int)

    stop_message = services.stop_service("svc_life")
    assert "stopped" in stop_message

    status = services.get_service_status("svc_life")
    assert status["is_running"] is False
    assert status["pid"] is None

    services.unregister_service("svc_life")
    with pytest.raises(services.ServiceNotFoundError):
        services.get_service_status("svc_life")


def test_unknown_service_raises(isolated_registry):
    services = isolated_registry
    with pytest.raises(services.ServiceNotFoundError):
        services.get_service_status("nope")


def test_format_server_status_handles_stopped_service(isolated_registry):
    """format_server_status must not KeyError on missing cwd/health fields."""
    from harness.agent.server_manager import (
        format_server_status,
        get_fastapi_server_status,
    )

    services = isolated_registry
    services.register_service(
        "fastapi_backend",
        command=["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8123"],
        cwd=None,
        health_url="http://127.0.0.1:8123/health",
        description="test server",
    )

    text = format_server_status(get_fastapi_server_status("fastapi_backend"))
    assert "Service: fastapi_backend" in text
    assert "Port 8123" in text
    assert "Process Running: No" in text


def test_start_fastapi_server_requires_app_main(isolated_registry, tmp_path):
    from harness.agent.server_manager import start_fastapi_server

    empty = tmp_path / "empty_backend"
    empty.mkdir()
    with pytest.raises(ValueError, match="app/main.py"):
        start_fastapi_server(backend_path=empty)


def test_service_tools_are_registered_in_pool():
    from harness.tools.registry import BUILTIN_HANDLERS, BUILTIN_TOOLS

    expected = {
        "start_server",
        "stop_server",
        "restart_server",
        "server_status",
        "remove_server",
        "list_services",
    }
    tool_names = {tool["name"] for tool in BUILTIN_TOOLS}
    assert expected <= tool_names
    assert expected <= set(BUILTIN_HANDLERS)


def test_service_tool_handlers_return_errors_not_raise(isolated_registry):
    """Handlers must degrade to an 'Error:' string instead of raising."""
    from harness.tools import registry

    assert registry.run_server_status("missing").startswith("Error:")
    assert registry.run_stop_server("missing").startswith("Error:")
    assert registry.run_list_services() == "No registered services."
