"""
Server manager for FastAPI/uvicorn backend services.

Provides high-level commands for managing the FastAPI backend server,
wrapping the generic Layer 4 service management infrastructure.
"""

import socket
from pathlib import Path
from typing import Any

from .services import (
    ServiceAlreadyExistsError,
    ServiceNotFoundError,
    get_service_status,
    register_service,
    restart_service,
    start_service,
    stop_service,
    unregister_service,
)

# Default service ID for the FastAPI backend
DEFAULT_SERVICE_ID = "fastapi_backend"


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """
    Check if a port is in use.
    
    Args:
        port: Port number to check
        host: Host address (default: 127.0.0.1)
        
    Returns:
        True if port is in use, False otherwise
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True


def start_fastapi_server(
    service_id: str = DEFAULT_SERVICE_ID,
    backend_path: Path | str | None = None,
    port: int = 8000,
    host: str = "127.0.0.1",
    health_endpoint: str = "/health",
    health_probe_retries: int = 10,
) -> str:
    """
    Start the FastAPI backend server.
    
    Args:
        service_id: Unique identifier for this service instance
        backend_path: Path to the backend directory (containing app/)
        port: Port to run the server on (default: 8000)
        host: Host to bind to (default: 127.0.0.1)
        health_endpoint: Health check endpoint path (default: /health)
        health_probe_retries: Number of health check retries (default: 10)
        
    Returns:
        Status message
        
    Raises:
        ServiceAlreadyExistsError: If service is already registered
        ValueError: If backend path is invalid or not provided
    """
    # Resolve backend path: explicit path, else auto-detect under cwd.
    candidate = Path(backend_path).resolve() if backend_path else Path.cwd()
    backend_dir = candidate if candidate.is_dir() else candidate.parent
    if not backend_dir.is_dir():
        raise ValueError(f"Backend directory does not exist: {backend_dir}")

    if not (backend_dir / "app" / "main.py").is_file():
        raise ValueError(
            f"No app/main.py found under {backend_dir}. "
            "Pass backend_path pointing at the backend repository root."
        )
    
    # Construct uvicorn command
    command = [
        "uvicorn",
        "app.main:app",
        "--host", host,
        "--port", str(port),
        "--log-level", "info",
    ]
    
    # Construct health check URL
    health_url = f"http://{host}:{port}{health_endpoint}"
    
    # Register the service if not already registered
    try:
        register_service(
            service_id=service_id,
            command=command,
            cwd=str(backend_dir),
            health_url=health_url,
            description=f"FastAPI backend server on {host}:{port}",
        )
    except ServiceAlreadyExistsError:
        # Already registered: refresh the spec when the service is not running
        # so a changed port/host/path takes effect on the next start.
        current = get_service_status(service_id)
        if not current.get("is_running") and current.get("command") != command:
            unregister_service(service_id, force=True)
            register_service(
                service_id=service_id,
                command=command,
                cwd=str(backend_dir),
                health_url=health_url,
                description=f"FastAPI backend server on {host}:{port}",
            )

    # Start the service
    return start_service(service_id, health_probe_retries=health_probe_retries)


def stop_fastapi_server(service_id: str = DEFAULT_SERVICE_ID) -> str:
    """
    Stop the FastAPI backend server.
    
    Args:
        service_id: Service identifier (default: fastapi_backend)
        
    Returns:
        Status message
        
    Raises:
        ServiceNotFoundError: If service is not registered
    """
    return stop_service(service_id)


def restart_fastapi_server(service_id: str = DEFAULT_SERVICE_ID) -> str:
    """
    Restart the FastAPI backend server.
    
    Args:
        service_id: Service identifier (default: fastapi_backend)
        
    Returns:
        Status message
        
    Raises:
        ServiceNotFoundError: If service is not registered
    """
    return restart_service(service_id)


def get_fastapi_server_status(service_id: str = DEFAULT_SERVICE_ID) -> dict[str, Any]:
    """
    Get detailed status of the FastAPI backend server.
    
    Includes:
    - Service metadata (command, cwd, description)
    - PID and process running status
    - Port binding status
    - Health endpoint status
    
    Args:
        service_id: Service identifier (default: fastapi_backend)
        
    Returns:
        Dictionary with status information:
        - service_id: str
        - description: str
        - command: list[str]
        - cwd: str
        - pid: int | None
        - is_running: bool
        - port_in_use: bool
        - health_url: str | None
        - health_status: str | None (e.g., "OK (status=200)", "Unreachable (ConnectionError)")
        
    Raises:
        ServiceNotFoundError: If service is not registered
    """
    # Get base status from service manager
    status = get_service_status(service_id)
    
    # Extract port from command for port checking
    port = None
    command = status.get("command", [])
    try:
        port_idx = command.index("--port")
        if port_idx + 1 < len(command):
            port = int(command[port_idx + 1])
    except (ValueError, IndexError):
        port = 8000  # Default port
    
    # Check port status
    status["port"] = port
    status["port_in_use"] = _is_port_in_use(port)
    
    return status


def remove_fastapi_server(service_id: str = DEFAULT_SERVICE_ID, force: bool = False) -> str:
    """
    Remove the FastAPI backend service registration.
    
    Args:
        service_id: Service identifier (default: fastapi_backend)
        force: If True, stop the service before removing (default: False)
        
    Returns:
        Status message
        
    Raises:
        ServiceNotFoundError: If service is not registered
        ServiceError: If service is running and force=False
    """
    return unregister_service(service_id, force=force)


def format_server_status(status: dict[str, Any]) -> str:
    """
    Format server status dictionary into human-readable string.
    
    Args:
        status: Status dictionary from get_fastapi_server_status()
        
    Returns:
        Formatted status string
    """
    lines = [
        f"Service: {status.get('service_id', 'unknown')}",
        f"Description: {status.get('description') or '(none)'}",
        f"Command: {' '.join(status.get('command') or [])}",
        f"Working Directory: {status.get('cwd') or '(inherited)'}",
        "",
        f"PID: {status.get('pid') if status.get('pid') else 'N/A'}",
        f"Process Running: {'Yes' if status.get('is_running') else 'No'}",
        f"Port {status.get('port', 'N/A')}: "
        f"{'In Use' if status.get('port_in_use') else 'Available'}",
    ]

    if status.get("health_url"):
        lines.append(f"Health URL: {status['health_url']}")
        health_check = status.get("health_check") or {}
        lines.append(
            f"Health Status: {health_check.get('message', status.get('health_status', 'Unknown'))}"
        )
    else:
        lines.append("Health Check: not configured")

    return "\n".join(lines)
