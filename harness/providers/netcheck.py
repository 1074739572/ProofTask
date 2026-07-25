"""Lightweight network / proxy diagnostics for clearer API failures."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse


def configured_proxy_url() -> str:
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("ALL_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
        or os.getenv("all_proxy")
        or ""
    ).strip()


def proxy_host_port(proxy_url: str) -> tuple[str, int] | None:
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, int(port)


def is_tcp_open(host: str, port: int, *, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_health_warning() -> str | None:
    """Return a user-facing warning if proxy env points at a dead local port."""
    proxy = configured_proxy_url()
    if not proxy:
        return None
    hp = proxy_host_port(proxy)
    if hp is None:
        return None
    host, port = hp
    # Only probe loopback proxies: checking remote proxies on every startup
    # would add latency and could report transient external network failures.
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    if is_tcp_open(host, port):
        return None
    return (
        f"代理 {proxy} 未监听（常见于 Clash/V2Ray 未启动）。"
        "当前 DeepSeek 等国内 API 可直连：关掉系统代理环境变量，或先打开代理软件。"
    )
