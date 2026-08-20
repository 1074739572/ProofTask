"""Agent type registry (config/agents.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from harness.models import get_model_profile, list_models
from harness.providers.config import get_provider, resolve_api_key

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_CONFIG_PATH = PACKAGE_ROOT / "config" / "agents.json"


@dataclass(frozen=True)
class AgentProfile:
    id: str
    model_id: str
    reasoning_effort: str | None
    label: str
    tools: list[str]
    system: str


@dataclass(frozen=True)
class AgentModelBinding:
    """The resolved, machine-checkable model route for one agent role.

    Goal uses several roles in one run.  Keeping this result structured makes
    a preflight report useful to both the CLI and the TUI instead of reducing
    the first bad route to a generic connection error.
    """

    agent_type: str
    model_id: str = ""
    provider_id: str = ""
    provider_label: str = ""
    api_key_env: str = ""
    api_key_configured: bool = False
    reasoning_effort: str | None = None
    supported_efforts: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_type": self.agent_type,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "provider_label": self.provider_label,
            "api_key_env": self.api_key_env,
            "api_key_configured": self.api_key_configured,
            "reasoning_effort": self.reasoning_effort,
            "supported_efforts": list(self.supported_efforts),
            "error": self.error,
        }


def _load_config() -> dict:
    if not AGENTS_CONFIG_PATH.exists():
        return {"agents": {}}
    return json.loads(AGENTS_CONFIG_PATH.read_text(encoding="utf-8"))


def list_agent_types() -> list[str]:
    return list(_load_config().get("agents", {}).keys())


def lead_model_hint() -> str | None:
    return _load_config().get("lead_model_hint")


def get_agent_profile(agent_type: str) -> AgentProfile | None:
    entry = _load_config().get("agents", {}).get(agent_type)
    if not entry:
        return None
    raw_effort = str(entry.get("reasoning_effort") or "").strip().lower()
    if raw_effort in {"", "none", "off", "default", "model-default", "model default"}:
        raw_effort = ""
    return AgentProfile(
        id=agent_type,
        model_id=entry["model_id"],
        reasoning_effort=raw_effort or None,
        label=entry.get("label", agent_type),
        tools=list(entry.get("tools", [])),
        system=entry.get("system", "Complete the task and return a summary."),
    )


def agent_descriptions() -> str:
    lines = []
    for agent_id in list_agent_types():
        profile = get_agent_profile(agent_id)
        if profile is None:
            continue
        model = get_model_profile(profile.model_id)
        lines.append(
            f"- {agent_id}: {profile.label} → model {profile.model_id} ({model.label})"
        )
    return "\n".join(lines)


def inspect_agent_model(
    agent_type: str,
    *,
    reasoning_effort: str | None = None,
) -> AgentModelBinding:
    """Resolve and validate an agent route without making a network request."""

    profile = get_agent_profile(agent_type)
    if profile is None:
        return AgentModelBinding(
            agent_type=agent_type,
            error=f"Unknown agent_type '{agent_type}'. Available: {', '.join(list_agent_types())}",
        )
    known_models = {str(model.get("id")) for model in list_models()}
    if profile.model_id not in known_models:
        return AgentModelBinding(
            agent_type=agent_type,
            model_id=profile.model_id,
            error=(
                f"Agent '{agent_type}' references unknown model '{profile.model_id}'. "
                "Add it to config/models.json before using this agent."
            ),
        )
    model_profile = get_model_profile(profile.model_id)
    selected_effort = str(
        reasoning_effort if reasoning_effort is not None else (profile.reasoning_effort or "")
    ).strip().lower()
    if selected_effort in {"", "none", "off", "default", "model-default", "model default"}:
        selected_effort = ""
    base = {
        "agent_type": agent_type,
        "model_id": profile.model_id,
        "reasoning_effort": selected_effort or None,
        "supported_efforts": tuple(model_profile.effort_options),
    }
    if selected_effort and selected_effort not in model_profile.effort_options:
        available = ", ".join(model_profile.effort_options) or "none"
        return AgentModelBinding(
            **base,
            error=(
                f"Agent '{agent_type}' requests reasoning_effort={selected_effort!r} "
                f"for {profile.model_id}, but supported values are: {available}."
            ),
        )
    try:
        provider = get_provider(model_profile.provider)
    except KeyError:
        return AgentModelBinding(
            **base,
            provider_id=model_profile.provider,
            error=(
                f"Agent '{agent_type}' uses model {profile.model_id} with unknown "
                f"provider '{model_profile.provider}'."
            ),
        )
    key_configured = bool(resolve_api_key(provider))
    if not key_configured:
        return AgentModelBinding(
            **base,
            provider_id=provider.id,
            provider_label=provider.label,
            api_key_env=provider.api_key_env,
            api_key_configured=False,
            error=(
                f"Agent '{agent_type}' needs model {profile.model_id} "
                f"but API key for {provider.label} is missing ({provider.api_key_env})."
            ),
        )
    return AgentModelBinding(
        **base,
        provider_id=provider.id,
        provider_label=provider.label,
        api_key_env=provider.api_key_env,
        api_key_configured=True,
    )


def validate_agent_model(
    agent_type: str,
    *,
    reasoning_effort: str | None = None,
) -> str | None:
    """Return a human-readable static route error, or ``None`` when valid."""

    return inspect_agent_model(agent_type, reasoning_effort=reasoning_effort).error


def agent_model_matrix(agent_types: Iterable[str] | None = None) -> list[AgentModelBinding]:
    """Inspect several roles in deterministic order for Goal preflight/UI."""

    selected = list(agent_types) if agent_types is not None else list_agent_types()
    return [inspect_agent_model(agent_type) for agent_type in selected]


def validate_agent_models(agent_types: Iterable[str]) -> list[str]:
    """Return all static route errors instead of stopping at the first one."""

    return [
        binding.error
        for binding in agent_model_matrix(agent_types)
        if binding.error
    ]
