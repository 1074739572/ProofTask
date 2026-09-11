"""
Layer 4: Durable Service Management
Session-independent service lifecycle with PID tracking, health probes, and log management.
"""

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import requests

# Service registry file (persistent across sessions)
SERVICES_DIR = Path.home() / ".improved_harness" / "services"
SERVICES_DIR.mkdir(parents=True, exist_ok=True)

# Global lock for service registry access.
# Re-entrant: helpers like list_services()/unregister_service() call other
# locked functions while already holding the lock.
_service_lock = threading.RLock()


class ServiceError(Exception):
    """Base exception for service management errors."""
    pass


class ServiceNotFoundError(ServiceError):
    """Service does not exist in registry."""
    pass


class ServiceAlreadyExistsError(ServiceError):
    """Service already registered."""
    pass


def _get_service_file(service_id: str) -> Path:
    """Get the JSON file path for a service."""
    return SERVICES_DIR / f"{service_id}.json"


def _get_pid_file(service_id: str) -> Path:
    """Get the PID file path for a service."""
    return SERVICES_DIR / f"{service_id}.pid"


def _get_log_file(service_id: str, stream: str) -> Path:
    """Get the log file path for a service (stdout or stderr)."""
    return SERVICES_DIR / f"{service_id}.{stream}.log"


def _load_service(service_id: str) -> dict[str, Any]:
    """Load service metadata from registry."""
    service_file = _get_service_file(service_id)
    if not service_file.exists():
        raise ServiceNotFoundError(f"Service '{service_id}' not found")
    
    with open(service_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_service(service_id: str, metadata: dict[str, Any]) -> None:
    """Save service metadata to registry."""
    service_file = _get_service_file(service_id)
    with open(service_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


def _delete_service(service_id: str) -> None:
    """Delete service metadata from registry."""
    service_file = _get_service_file(service_id)
    if service_file.exists():
        service_file.unlink()


def _read_pid(service_id: str) -> int | None:
    """Read PID from PID file."""
    pid_file = _get_pid_file(service_id)
    if not pid_file.exists():
        return None
    
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def _write_pid(service_id: str, pid: int) -> None:
    """Write PID to PID file."""
    pid_file = _get_pid_file(service_id)
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _delete_pid(service_id: str) -> None:
    """Delete PID file."""
    pid_file = _get_pid_file(service_id)
    if pid_file.exists():
        pid_file.unlink()


def _is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        process = psutil.Process(pid)
        return process.is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _kill_process_tree(pid: int, timeout: int = 5) -> None:
    """Kill a process and all its descendants."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        
        # Terminate parent and children
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        try:
            parent.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        
        # Wait for graceful termination
        gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)
        
        # Force kill if still alive
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _check_health(health_url: str, timeout: int = 2) -> tuple[bool, str]:
    """
    Check service health endpoint.
    Returns (is_healthy, message).
    """
    try:
        response = requests.get(health_url, timeout=timeout)
        if response.status_code == 200:
            return True, f"OK (status={response.status_code})"
        else:
            return False, f"Unhealthy (status={response.status_code})"
    except requests.RequestException as e:
        return False, f"Unreachable ({type(e).__name__})"


def register_service(
    service_id: str,
    command: list[str],
    cwd: str | None = None,
    health_url: str | None = None,
    description: str = "",
) -> str:
    """
    Register a new durable service.
    Does not start the service - use start_service() for that.
    """
    with _service_lock:
        if _get_service_file(service_id).exists():
            raise ServiceAlreadyExistsError(f"Service '{service_id}' already exists")
        
        metadata = {
            "service_id": service_id,
            "command": command,
            "cwd": cwd,
            "health_url": health_url,
            "description": description,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        
        _save_service(service_id, metadata)
        return f"Service '{service_id}' registered successfully"


def start_service(service_id: str, health_probe_retries: int = 10) -> str:
    """
    Start a registered service.
    - Launches process with detached console
    - Records PID
    - Redirects stdout/stderr to log files
    - Polls health endpoint if configured
    """
    with _service_lock:
        metadata = _load_service(service_id)
        
        # Check if already running
        existing_pid = _read_pid(service_id)
        if existing_pid and _is_process_running(existing_pid):
            return f"Service '{service_id}' is already running (PID={existing_pid})"
        
        # Prepare log files
        stdout_log = _get_log_file(service_id, "stdout")
        stderr_log = _get_log_file(service_id, "stderr")
        
        # Launch process (detached from current session)
        with open(stdout_log, "a", encoding="utf-8") as stdout_file, \
             open(stderr_log, "a", encoding="utf-8") as stderr_file:
            
            # Windows: CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS
            # This prevents the child from being killed when parent dies
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            
            process = subprocess.Popen(
                metadata["command"],
                cwd=metadata.get("cwd"),
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            
            pid = process.pid
            _write_pid(service_id, pid)
        
        # Update metadata
        metadata["started_at"] = datetime.now(timezone.utc).isoformat()
        metadata["pid"] = pid
        _save_service(service_id, metadata)
        
        # Health probe if configured
        health_url = metadata.get("health_url")
        if health_url:
            health_ok = False
            for attempt in range(1, health_probe_retries + 1):
                time.sleep(1)  # Wait before each probe
                is_healthy, msg = _check_health(health_url)
                if is_healthy:
                    health_ok = True
                    break
            
            if health_ok:
                return f"Service '{service_id}' started successfully (PID={pid}, health check passed)"
            else:
                return f"Service '{service_id}' started (PID={pid}) but health check failed after {health_probe_retries} attempts"
        
        return f"Service '{service_id}' started successfully (PID={pid})"


def stop_service(service_id: str) -> str:
    """
    Stop a running service.
    - Terminates process tree
    - Cleans up PID file
    """
    with _service_lock:
        metadata = _load_service(service_id)
        
        pid = _read_pid(service_id)
        if not pid:
            return f"Service '{service_id}' is not running (no PID file)"
        
        if not _is_process_running(pid):
            _delete_pid(service_id)
            return f"Service '{service_id}' was not running (PID={pid} not found)"
        
        # Kill process tree
        _kill_process_tree(pid)
        
        # Clean up PID file
        _delete_pid(service_id)
        
        # Update metadata
        metadata["stopped_at"] = datetime.now(timezone.utc).isoformat()
        metadata.pop("pid", None)
        _save_service(service_id, metadata)
        
        return f"Service '{service_id}' stopped successfully (PID={pid})"


def restart_service(service_id: str) -> str:
    """
    Restart a service (stop + start).
    """
    stop_result = stop_service(service_id)
    time.sleep(1)  # Brief pause between stop and start
    start_result = start_service(service_id)
    return f"{stop_result}\n{start_result}"


def get_service_status(service_id: str) -> dict[str, Any]:
    """
    Get detailed status of a service.
    - Checks PID
    - Checks process existence
    - Checks health endpoint if configured
    """
    with _service_lock:
        metadata = _load_service(service_id)
        
        pid = _read_pid(service_id)
        is_running = pid is not None and _is_process_running(pid)
        
        status = {
            "service_id": service_id,
            "description": metadata.get("description", ""),
            "command": metadata["command"],
            "cwd": metadata.get("cwd"),
            "health_url": metadata.get("health_url"),
            "pid": pid,
            "is_running": is_running,
            "running": is_running,
            "registered_at": metadata.get("registered_at"),
            "started_at": metadata.get("started_at"),
            "stopped_at": metadata.get("stopped_at"),
        }
        
        # Health check if running and configured
        health_url = metadata.get("health_url")
        if is_running and health_url:
            is_healthy, health_msg = _check_health(health_url)
            status["health_check"] = {
                "url": health_url,
                "is_healthy": is_healthy,
                "message": health_msg,
            }
            status["health_status"] = health_msg
        
        return status


def list_services() -> list[dict[str, Any]]:
    """List all registered services with their status."""
    with _service_lock:
        services = []
        for service_file in SERVICES_DIR.glob("*.json"):
            service_id = service_file.stem
            try:
                status = get_service_status(service_id)
                services.append(status)
            except ServiceNotFoundError:
                pass  # Skip if metadata file was deleted concurrently
        
        return services


def unregister_service(service_id: str, force: bool = False) -> str:
    """
    Unregister a service from the registry.
    If force=True, stops the service first if running.
    """
    with _service_lock:
        metadata = _load_service(service_id)
        
        pid = _read_pid(service_id)
        is_running = pid is not None and _is_process_running(pid)
        
        if is_running and not force:
            return f"Service '{service_id}' is still running (PID={pid}). Stop it first or use force=True"
        
        if is_running:
            stop_service(service_id)
        
        # Clean up all files
        _delete_service(service_id)
        _delete_pid(service_id)
        
        return f"Service '{service_id}' unregistered successfully"
