"""MCP connection manager and tool pool assembly."""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

from harness.mcp.base import MCPClientProtocol, MockMCPClient
from harness.mcp.client import create_real_client
from harness.mcp.config import load_mcp_config
from harness.mcp.mock import MOCK_SERVERS

mcp_clients: dict[str, MCPClientProtocol] = {}
mcp_tool_meta: dict[str, dict] = {}

_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# Background bootstrap state: startup no longer blocks on MCP. Servers connect
# in a daemon thread (parallel), and the *first* agent turn waits briefly
# (ensure_mcp_ready) so the tool pool is ready before the first LLM call.
_bootstrap_lock = threading.Lock()
_bootstrap_done = threading.Event()
_bootstrap_results: list[str] = []
_bootstrap_started = False
_barrier_lock = threading.Lock()
_barrier_consumed = False
_warnings_consumed = False


def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED_CHARS.sub("_", name)


def connect_mcp(name: str) -> str:
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"

    config = load_mcp_config()
    if name in config:
        if name == "github":
            import os

            token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            if not token:
                return (
                    "MCP 'github' skipped: set GITHUB_PERSONAL_ACCESS_TOKEN in .env "
                    "(and ensure Docker is running)."
                )
        try:
            client = create_real_client(name, config[name])
            mcp_clients[name] = client
            tool_names = [tool["name"] for tool in client.tools]
            # Success stays quiet — tools are available in the pool; avoid spam on every launch.
            return (
                f"Connected to MCP server '{name}' (stdio). "
                f"Discovered {len(client.tools)} tools: {', '.join(tool_names)}"
            )
        except ImportError:
            return (
                "MCP SDK not installed. Run: pip install mcp\n"
                f"Then retry connect_mcp('{name}')."
            )
        except Exception as exc:
            return f"MCP connection failed ({name}): {exc}"

    factory = MOCK_SERVERS.get(name)
    if factory:
        client = factory()
        mcp_clients[name] = client
        tool_names = [tool["name"] for tool in client.tools]
        return (
            f"Connected to mock MCP server '{name}'. "
            f"Discovered {len(client.tools)} tools: {', '.join(tool_names)}"
        )

    configured = ", ".join(config.keys()) or "(none)"
    mock_names = ", ".join(MOCK_SERVERS.keys())
    return (
        f"Unknown server '{name}'. "
        f"Configured in config/mcp.json: {configured}. "
        f"Mock servers: {mock_names}"
    )


def bootstrap_mcp_servers() -> list[str]:
    """Connect every server in mcp.json. Returns status lines (success + failures)."""
    return [connect_mcp(name) for name in load_mcp_config()]


def bootstrap_mcp_servers_async() -> None:
    """Start connecting every server in mcp.json in the background (non-blocking).

    Results feed the first-turn barrier (ensure_mcp_ready) and the one-shot
    warnings (take_mcp_bootstrap_warnings). Servers connect in parallel.
    """
    global _bootstrap_started
    with _bootstrap_lock:
        if _bootstrap_started:
            return
        _bootstrap_started = True
        names = load_mcp_config()
    if not names:
        _bootstrap_done.set()
        return
    threading.Thread(
        target=_bootstrap_worker,
        args=(names,),
        daemon=True,
        name="mcp-bootstrap",
    ).start()


def _bootstrap_worker(names: list[str]) -> None:
    with ThreadPoolExecutor(max_workers=max(2, len(names))) as pool:
        results = list(pool.map(connect_mcp, names))
    with _bootstrap_lock:
        _bootstrap_results[:] = results
    _bootstrap_done.set()


def wait_mcp_bootstrap(timeout: float = 3.0) -> list[str]:
    """Block until the background bootstrap finishes or *timeout* seconds elapse.

    Returns the results collected so far (partial if the timeout won).
    """
    _bootstrap_done.wait(timeout)
    with _bootstrap_lock:
        return list(_bootstrap_results)


def ensure_mcp_ready(timeout: float = 3.0) -> None:
    """First-use barrier: wait up to *timeout* for the background bootstrap.

    Called at the start of the first agent turn; later turns return instantly.
    If the bootstrap never started (e.g. tests that skip the CLI entrypoint)
    this returns immediately.
    """
    global _barrier_consumed
    with _barrier_lock:
        if _barrier_consumed:
            return
        _barrier_consumed = True
    with _bootstrap_lock:
        started = _bootstrap_started
    if not started:
        return
    _bootstrap_done.wait(timeout)


def take_mcp_bootstrap_warnings() -> list[str]:
    """Return bootstrap failure/skip warnings exactly once, once finished.

    Startup prints nothing while MCP connects; the first turn absorbs the
    wait, then surfaces any failures/skips (successes stay quiet).
    """
    global _warnings_consumed
    with _barrier_lock:
        if _warnings_consumed:
            return []
        if not _bootstrap_done.is_set():
            return []
        _warnings_consumed = True
        results = list(_bootstrap_results)
    return mcp_bootstrap_warnings(results)


def mcp_bootstrap_warnings(results: list[str]) -> list[str]:
    """Lines worth showing at startup — failures / skips only."""
    warnings: list[str] = []
    for line in results:
        text = line.strip()
        if not text:
            continue
        lower = text.lower()
        if lower.startswith("connected"):
            continue
        if "already connected" in lower:
            continue
        warnings.append(text)
    return warnings


def assemble_tool_pool(
    builtin_tools: list[dict],
    builtin_handlers: dict,
) -> tuple[list[dict], dict]:
    tools = list(builtin_tools)
    handlers = dict(builtin_handlers)
    mcp_tool_meta.clear()

    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append(
                {
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "input_schema": tool_def.get("inputSchema", {}),
                }
            )
            mcp_tool_meta[prefixed] = {
                "destructive": bool(tool_def.get("destructive")),
                "readOnly": bool(tool_def.get("readOnly")),
                "server": server_name,
                "tool": tool_def["name"],
            }
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw)
            )
    return tools, handlers
