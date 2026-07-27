"""Load provider definitions from config/providers.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
PROVIDERS_CONFIG_PATH = PACKAGE_ROOT / "config" / "providers.json"


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    label: str
    type: str
    base_url: str
    api_key_env: str
    api_key_fallback_env: str | None = None


def _timeout_value(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(0.1, value)


def provider_timeout() -> tuple[float, float, float, float]:
    """Return connect, read, write and pool timeouts in seconds."""
    return (
        _timeout_value("HARNESS_LLM_CONNECT_TIMEOUT", 10.0),
        _timeout_value("HARNESS_LLM_READ_TIMEOUT", 90.0),
        _timeout_value("HARNESS_LLM_WRITE_TIMEOUT", 30.0),
        _timeout_value("HARNESS_LLM_POOL_TIMEOUT", 10.0),
    )


def load_providers() -> dict[str, ProviderConfig]:
    if not PROVIDERS_CONFIG_PATH.exists():
        return {
            "deepseek": ProviderConfig(
                id="deepseek",
                label="DeepSeek",
                type="anthropic",
                base_url=os.getenv(
                    "ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
                ),
                api_key_env="DEEPSEEK_API_KEY",
                api_key_fallback_env="ANTHROPIC_API_KEY",
            )
        }

    data = json.loads(PROVIDERS_CONFIG_PATH.read_text(encoding="utf-8"))
    providers: dict[str, ProviderConfig] = {}
    for provider_id, entry in data.items():
        providers[provider_id] = ProviderConfig(
            id=provider_id,
            label=entry.get("label", provider_id),
            type=entry.get("type", "anthropic"),
            base_url=entry["base_url"],
            api_key_env=entry["api_key_env"],
            api_key_fallback_env=entry.get("api_key_fallback_env"),
        )
    return providers


def get_provider(provider_id: str) -> ProviderConfig:
    providers = load_providers()
    if provider_id not in providers:
        raise KeyError(f"Unknown provider '{provider_id}'")
    return providers[provider_id]


def resolve_api_key(provider: ProviderConfig) -> str | None:
    key = os.getenv(provider.api_key_env)
    if key:
        return key
    if provider.api_key_fallback_env:
        return os.getenv(provider.api_key_fallback_env)
    return None


def provider_key_status() -> dict[str, bool]:
    return {
        provider_id: bool(resolve_api_key(provider))
        for provider_id, provider in load_providers().items()
    }
