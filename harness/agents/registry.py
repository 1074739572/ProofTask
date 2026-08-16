"""Agent type registry (config/agents.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
    return AgentProfile(
        id=agent_type,
        model_id=entry["model_id"],
        reasoning_effort=str(entry.get("reasoning_effort") or "").strip().lower() or None,
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


def validate_agent_model(agent_type: str) -> str | None:
    profile = get_agent_profile(agent_type)
    if profile is None:
        return f"Unknown agent_type '{agent_type}'. Available: {', '.join(list_agent_types())}"
    known_models = {str(model.get("id")) for model in list_models()}
    if profile.model_id not in known_models:
        return (
            f"Agent '{agent_type}' references unknown model '{profile.model_id}'. "
            "Add it to config/models.json before using this agent."
        )
    model_profile = get_model_profile(profile.model_id)
    if profile.reasoning_effort:
        if profile.reasoning_effort not in model_profile.effort_options:
            available = ", ".join(model_profile.effort_options) or "none"
            return (
                f"Agent '{agent_type}' requests reasoning_effort={profile.reasoning_effort!r} "
                f"for {profile.model_id}, but supported values are: {available}."
            )
    try:
        provider = get_provider(model_profile.provider)
    except KeyError:
        return (
            f"Agent '{agent_type}' uses model {profile.model_id} with unknown "
            f"provider '{model_profile.provider}'."
        )
    if not resolve_api_key(provider):
        return (
            f"Agent '{agent_type}' needs model {profile.model_id} "
            f"but API key for {provider.label} is missing."
        )
    return None
