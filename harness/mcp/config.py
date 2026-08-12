"""Load MCP server definitions from config/mcp.json."""

from __future__ import annotations

import json
import os
import re

from harness.settings import get_mcp_config_path

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _expand_env_value(value: str) -> str:
    """Resolve ``${VAR}`` from the process environment; leave other strings as-is."""
    match = _ENV_REF.match(value.strip())
    if not match:
        return value
    return os.getenv(match.group(1), "")


def resolve_server_env(server_cfg: dict) -> dict | None:
    """Build child-process env: inherit os.environ, overlay expanded mcp.json env."""
    raw = server_cfg.get("env")
    if not raw:
        return None
    merged = dict(os.environ)
    for key, value in raw.items():
        if isinstance(value, str):
            merged[key] = _expand_env_value(value)
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    return merged


def load_mcp_config() -> dict[str, dict]:
    path = get_mcp_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get("mcpServers", {})
