"""Anthropic SDK client factory per provider."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import httpx

from harness.providers.config import ProviderConfig, provider_timeout, resolve_api_key

if TYPE_CHECKING:
    from anthropic import Anthropic

_lock = threading.Lock()
_clients: dict[str, "Anthropic"] = {}


def get_anthropic_client(provider: ProviderConfig) -> "Anthropic":
    with _lock:
        cached = _clients.get(provider.id)
        if cached is not None:
            return cached
        # Lazy import: the Anthropic SDK is ~7s to import on Windows; only pay
        # that cost when a turn actually needs an Anthropic client.
        from anthropic import Anthropic

        api_key = resolve_api_key(provider)
        if not api_key:
            envs = provider.api_key_env
            if provider.api_key_fallback_env:
                envs += f" or {provider.api_key_fallback_env}"
            raise RuntimeError(
                f"Missing API key for provider '{provider.label}'. "
                f"Set {envs} in .env"
            )
        connect, read, write, pool = provider_timeout()
        client = Anthropic(
            api_key=api_key,
            base_url=provider.base_url,
            timeout=httpx.Timeout(read, connect=connect, write=write, pool=pool),
        )
        _clients[provider.id] = client
        return client
