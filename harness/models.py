"""Thread-safe runtime model selection backed by config/models.json."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from harness.providers.config import get_provider, provider_key_status, resolve_api_key

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MODELS_CONFIG_PATH = PACKAGE_ROOT / "config" / "models.json"

_lock = threading.RLock()
_current_model: str = ""
_current_effort: str | None = None
_catalog: list[dict] = []
_default_model: str = ""

EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_ALIASES = {
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "x high": "xhigh",
    "x-high": "xhigh",
}
OFF_ALIASES = {"", "off", "default", "model-default", "model default"}
EFFORT_DETAILS = {
    "none": "disable reasoning where the model supports it",
    "minimal": "lowest reasoning budget",
    "low": "cheap/fast reasoning",
    "medium": "balanced reasoning",
    "high": "stronger reasoning",
    "xhigh": "extra-high reasoning",
    "max": "maximum reasoning",
    "ultra": "ultra reasoning",
}


def _normalize_effort(value: object) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in OFF_ALIASES:
        return None
    text = EFFORT_ALIASES.get(text, text)
    return text if text in EFFORT_ORDER else None


def _normalize_effort_options(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw = [part.strip() for part in values.split(",")]
    elif isinstance(values, (list, tuple)):
        raw = [str(part).strip() for part in values]
    else:
        raw = []
    allowed = {_normalize_effort(part) for part in raw}
    return tuple(effort for effort in EFFORT_ORDER if effort in allowed)


def _effort_display(effort: str) -> str:
    return "extra high" if effort == "xhigh" else effort


def _effort_options_text(options: tuple[str, ...]) -> str:
    return ", ".join(_effort_display(opt) for opt in options) if options else "none declared"


def _normalize_api_effort_values(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in values.items():
        normalized = _normalize_effort(key)
        if normalized and value is not None:
            out[normalized] = str(value)
    return out


def api_reasoning_effort(profile: "ModelProfile") -> str | None:
    if not profile.reasoning_effort:
        return None
    return profile.api_effort_values.get(profile.reasoning_effort, profile.reasoning_effort)


@dataclass(frozen=True)
class ModelProfile:
    """Resolved model sent to the API (id may differ from api_model)."""

    id: str
    label: str
    provider: str
    api_model: str
    thinking: bool = False
    reasoning_effort: str | None = None
    effort_options: tuple[str, ...] = ()
    api_effort_values: dict[str, str] = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)
    context_window: int = 0


def _load_catalog() -> tuple[str, list[dict]]:
    if not MODELS_CONFIG_PATH.exists():
        fallback = os.getenv("MODEL_ID", "deepseek-v4-flash")
        return fallback, [
            {
                "id": fallback,
                "label": fallback,
                "provider": "deepseek",
                "api_model": fallback,
                "thinking": False,
                "reasoning_effort": None,
                "effort_options": [],
                "api_effort_values": {},
                "extra_body": {},
                "context_window": 1_000_000,
            }
        ]

    data = json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8"))
    default = data.get("default") or os.getenv("MODEL_ID", "deepseek-v4-flash")
    models = []
    for entry in data.get("models", []):
        model_id = entry.get("id")
        if not model_id:
            continue
        models.append(
            {
                "id": model_id,
                "label": entry.get("label", model_id),
                "provider": entry.get("provider", "deepseek"),
                "api_model": entry.get("api_model", model_id),
                "thinking": bool(entry.get("thinking")),
                "reasoning_effort": _normalize_effort(entry.get("reasoning_effort")),
                "effort_options": _normalize_effort_options(entry.get("effort_options")),
                "api_effort_values": _normalize_api_effort_values(entry.get("api_effort_values")),
                "extra_body": entry.get("extra_body") or {},
                "context_window": int(entry.get("context_window") or 0),
            }
        )
    if not models:
        models = [
            {
                "id": default,
                "label": default,
                "provider": "deepseek",
                "api_model": default,
                "thinking": False,
                "reasoning_effort": None,
                "effort_options": [],
                "extra_body": {},
                "context_window": 1_000_000,
            }
        ]
    return default, models


def _profile_from_entry(entry: dict) -> ModelProfile:
    return ModelProfile(
        id=entry["id"],
        label=entry["label"],
        provider=entry.get("provider", "deepseek"),
        api_model=entry.get("api_model", entry["id"]),
        thinking=bool(entry.get("thinking")),
        reasoning_effort=entry.get("reasoning_effort"),
        effort_options=tuple(entry.get("effort_options") or ()),
        api_effort_values=dict(entry.get("api_effort_values") or {}),
        extra_body=dict(entry.get("extra_body") or {}),
        context_window=int(entry.get("context_window") or 0),
    )


def initialize_model(override: str | None = None) -> str:
    """Resolve initial model from CLI override, .env MODEL_ID, or config default."""
    global _current_model, _catalog, _default_model

    config_default, catalog = _load_catalog()
    env_model = os.getenv("MODEL_ID")
    initial = override or env_model or config_default

    with _lock:
        _catalog = catalog
        _default_model = config_default
        known_ids = {entry["id"] for entry in _catalog}
        if initial not in known_ids:
            _catalog = list(_catalog) + [
                {
                    "id": initial,
                    "label": f"{initial} (custom)",
                    "provider": "deepseek",
                    "api_model": initial,
                    "thinking": False,
                    "reasoning_effort": None,
                    "effort_options": [],
                    "api_effort_values": {},
                    "extra_body": {},
                    "context_window": 0,
                }
            ]
        if override is not None:
            _current_model = override
        elif not _current_model:
            _current_model = initial
        return _current_model


def get_model() -> str:
    with _lock:
        if not _current_model:
            return initialize_model()
        return _current_model


def get_model_profile(
    model_id: str | None = None,
    *,
    effort_override: str | None = None,
    inherit_interactive_effort: bool = True,
) -> ModelProfile:
    """Return a model profile, optionally with a call-scoped effort override.

    A scoped override is used by durable workflows such as Goal mode.  It must
    not mutate the user's interactive ``/effort`` selection.
    """
    with _lock:
        if not _catalog:
            initialize_model()
        mid = model_id or _current_model or _default_model
        for entry in _catalog:
            if entry["id"] == mid:
                profile = _profile_from_entry(entry)
                selected_effort = _normalize_effort(effort_override) if effort_override else (
                    _current_effort
                    if inherit_interactive_effort and (model_id is None or model_id == _current_model)
                    else None
                )
                if selected_effort:
                    return replace(profile, reasoning_effort=selected_effort)
                return profile
        profile = ModelProfile(
            id=mid,
            label=mid,
            provider="deepseek",
            api_model=mid,
            reasoning_effort=(
                _normalize_effort(effort_override)
                if effort_override
                else (_current_effort if inherit_interactive_effort else None)
            ),
            effort_options=(),
            api_effort_values={},
            extra_body={},
            context_window=0,
        )
        return profile


def _provider_label(provider_id: str) -> str:
    try:
        return get_provider(provider_id).label
    except KeyError:
        return provider_id


def set_model(model_id: str) -> str:
    global _current_model, _current_effort
    model_id = model_id.strip()
    if not model_id:
        return "Usage: /model <id>   or   /model list"

    with _lock:
        known_ids = {entry["id"] for entry in _catalog}
        if model_id not in known_ids:
            options = ", ".join(sorted(known_ids))
            return f"Unknown model '{model_id}'. Available: {options}"
        profile = _profile_from_entry(next(e for e in _catalog if e["id"] == model_id))
        provider = get_provider(profile.provider)
        if not resolve_api_key(provider):
            envs = provider.api_key_env
            if provider.api_key_fallback_env:
                envs += f" or {provider.api_key_fallback_env}"
            return (
                f"Cannot switch to {model_id}: missing API key for {provider.label}. "
                f"Set {envs} in .env"
            )
        _current_model = model_id
        effort_reset_note = ""
        if _current_effort and _current_effort not in profile.effort_options:
            effort_reset_note = f"; effort reset from {_current_effort} to model default"
            _current_effort = None
        api_note = ""
        if profile.api_model != profile.id:
            api_note = f" → API: {profile.api_model}"
        if profile.thinking:
            api_note += " (thinking on)"
        return (
            f"Switched to {model_id} ({profile.label}) "
            f"[{_provider_label(profile.provider)}]{api_note}{effort_reset_note}"
        )


def list_models() -> list[dict]:
    with _lock:
        if not _catalog:
            initialize_model()
        return list(_catalog)


def model_label(model_id: str | None = None) -> str:
    return get_model_profile(model_id).label


def format_model_status() -> str:
    profile = get_model_profile()
    provider_name = _provider_label(profile.provider)
    api = profile.api_model
    extra = f" [{provider_name}]"
    if api != profile.id:
        extra += f" → API: {api}"
    if profile.thinking:
        extra += " (thinking)"
    if profile.reasoning_effort:
        extra += f" (effort={profile.reasoning_effort})"
    elif profile.effort_options:
        extra += f" (effort options: {_effort_options_text(profile.effort_options)})"
    return f"Model: {profile.id}{extra}  —  /model to switch"


def format_model_list() -> str:
    current = get_model()
    key_status = provider_key_status()
    lines = [
        "Available models:",
        f"(Keys are read from {PACKAGE_ROOT / '.env'}, not .env.example)",
    ]

    by_provider: dict[str, list[dict]] = {}
    for entry in list_models():
        by_provider.setdefault(entry.get("provider", "deepseek"), []).append(entry)

    for provider_id, entries in by_provider.items():
        provider_label = _provider_label(provider_id)
        configured = key_status.get(provider_id, False)
        status = "key ok" if configured else "key missing"
        lines.append(f"\n[{provider_label}] ({status})")
        for entry in entries:
            marker = " *" if entry["id"] == current else "  "
            suffix = entry["label"]
            api = entry.get("api_model", entry["id"])
            has_effort_meta = entry.get("reasoning_effort") or entry.get("effort_options")
            if api != entry["id"] or entry.get("thinking") or has_effort_meta or entry.get("extra_body"):
                bits = [f"api={api}"]
                if entry.get("thinking"):
                    bits.append("thinking")
                if entry.get("reasoning_effort"):
                    bits.append(f"default effort={entry.get('reasoning_effort')}")
                if entry.get("effort_options"):
                    bits.append(f"efforts={_effort_options_text(tuple(entry.get('effort_options') or ()))}")
                if entry.get("extra_body"):
                    bits.append("extra_body")
                suffix += f" [{', '.join(bits)}]"
            lines.append(f"{marker} {entry['id']:<28} {suffix}")

    lines.append("")
    lines.append("Switch with: /model  (interactive)  or  /model <id>")
    lines.append(f"API keys: edit {PACKAGE_ROOT / '.env'}")
    return "\n".join(lines)


def get_reasoning_effort() -> str | None:
    with _lock:
        return _current_effort


def set_reasoning_effort(effort: str | None) -> str:
    global _current_effort
    value = str(effort or "").strip().lower().replace("_", "-")
    normalized = _normalize_effort(value)

    with _lock:
        profile = get_model_profile()
        options = profile.effort_options
        if value in OFF_ALIASES:
            _current_effort = None
            return "Reasoning effort: model default"
        if normalized is None:
            available = ", ".join(["off", *options]) or "off"
            return f"Unknown effort '{value}'. Available for {profile.id}: {available}"
        if not options:
            return (
                f"Model {profile.id} does not declare reasoning_effort support. "
                "Use /effort off, or add effort_options to config/models.json."
            )
        if normalized not in options:
            return (
                f"Effort '{_effort_display(normalized)}' is not supported by {profile.id}. "
                f"Available: off, {_effort_options_text(options)}"
            )
        _current_effort = normalized
        return f"Reasoning effort for {profile.id}: {_current_effort}"


def format_effort_list() -> str:
    profile = get_model_profile()
    current = get_reasoning_effort()
    current_display = _effort_display(current) if current else "model default"
    lines = [
        f"Reasoning effort options for {profile.id}:",
        "  off  — model default / no override",
    ]
    if profile.effort_options:
        for effort in profile.effort_options:
            marker = " *" if current == effort else "  "
            lines.append(f"{marker} {_effort_display(effort):<10} — {EFFORT_DETAILS.get(effort, '')}")
    else:
        lines.append("  (this model has no declared reasoning_effort options)")
    lines.append("")
    lines.append(f"Current: {current_display}")
    lines.append("Configure per-model options in config/models.json via effort_options.")
    return "\n".join(lines)


def handle_effort_command(query: str) -> str:
    parts = query.strip().split(maxsplit=1)
    if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker"):
        return format_effort_list()
    return set_reasoning_effort(parts[1])


def list_efforts() -> list[dict]:
    profile = get_model_profile()
    current = get_reasoning_effort() or "off"
    items = [
        {"id": "off", "label": "Model default", "detail": "no override", "current": current == "off"},
    ]
    for effort in profile.effort_options:
        label = "Extra High" if effort == "xhigh" else effort.title()
        items.append({
            "id": effort,
            "label": label,
            "detail": EFFORT_DETAILS.get(effort, ""),
            "current": current == effort,
        })
    if len(items) == 1:
        items[0]["detail"] = f"{profile.id} has no declared effort options"
    return items


def handle_model_command(query: str) -> str:
    parts = query.strip().split(maxsplit=1)
    if len(parts) == 1 or parts[1].lower() in ("list", "pick", "picker"):
        from harness.ui.model_picker import run_model_picker

        return run_model_picker()
    return set_model(parts[1])
