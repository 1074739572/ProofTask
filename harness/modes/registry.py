"""Load execution modes from config/modes.json (user-extensible)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
MODES_CONFIG_PATH = PACKAGE_ROOT / "config" / "modes.json"


@dataclass(frozen=True)
class ModeProfile:
    id: str
    label: str
    summary: str
    prompt: str
    lead_model_hint: str | None
    enable_task: bool
    disable_tools: frozenset[str]
    allowed_tools: frozenset[str] | None = None
    builtin_skills: tuple[str, ...] = ()
    confirm_before_execute: bool = False
    auto_route: bool = False


def _builtin_fallback() -> dict:
    return {
        "default": "direct",
        "modes": {
            "direct": {
                "label": "Direct",
                "summary": "Current model executes directly",
                "enable_task": False,
                "disable_tools": [],
                "prompt": "Execution mode: DIRECT\n- Execute work yourself.",
            }
        },
    }


def load_modes_config() -> dict:
    if not MODES_CONFIG_PATH.exists():
        return _builtin_fallback()
    data = json.loads(MODES_CONFIG_PATH.read_text(encoding="utf-8"))
    if "modes" not in data:
        return _builtin_fallback()
    return data


def list_mode_ids() -> list[str]:
    return list(load_modes_config().get("modes", {}).keys())


def default_mode_id() -> str:
    config = load_modes_config()
    default = config.get("default", "direct")
    if default in config.get("modes", {}):
        return default
    ids = list_mode_ids()
    return ids[0] if ids else "direct"


def get_mode_profile(mode_id: str) -> ModeProfile | None:
    entry = load_modes_config().get("modes", {}).get(mode_id)
    if not entry:
        return None
    hint = entry.get("lead_model_hint")
    skills = entry.get("builtin_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    return ModeProfile(
        id=mode_id,
        label=entry.get("label", mode_id),
        summary=entry.get("summary", ""),
        prompt=entry.get("prompt", f"Execution mode: {mode_id.upper()}"),
        lead_model_hint=hint if hint else None,
        enable_task=bool(entry.get("enable_task", False)),
        disable_tools=frozenset(entry.get("disable_tools") or []),
        allowed_tools=(
            frozenset(str(name).strip() for name in entry.get("allowed_tools", []) if str(name).strip())
            if "allowed_tools" in entry
            else None
        ),
        builtin_skills=tuple(str(name).strip() for name in skills if str(name).strip()),
        confirm_before_execute=bool(entry.get("confirm_before_execute", False)),
        auto_route=bool(entry.get("auto_route", False)),
    )


def format_mode_catalog() -> str:
    lines = ["Available modes (edit config/modes.json to add your own):"]
    for mode_id in list_mode_ids():
        profile = get_mode_profile(mode_id)
        if profile is None:
            continue
        lines.append(f"\n  {mode_id}")
        lines.append(f"    {profile.label}")
        if profile.summary:
            lines.append(f"    {profile.summary}")
        if profile.enable_task:
            lines.append("    task tool: on")
        if profile.auto_route:
            lines.append("    auto route: on")
        if profile.confirm_before_execute:
            lines.append("    confirm-before-execute: on (tools locked until 确认执行/go)")
        if profile.builtin_skills:
            lines.append(f"    builtin skills: {', '.join(profile.builtin_skills)}")
        if profile.disable_tools:
            lines.append(f"    disabled tools: {', '.join(sorted(profile.disable_tools))}")
        if profile.allowed_tools is not None:
            lines.append(f"    allowed tools: {', '.join(sorted(profile.allowed_tools))}")
    lines.append("\nSwitch: /mode <id>  or  /mode (picker)")
    lines.append("Template: config/modes.example.json")
    return "\n".join(lines)
