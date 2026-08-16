"""Session and persistent permission state."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from harness.settings import get_workspace_paths

PermissionEffect = Literal["allow", "ask", "deny"]

def _saved_rules_path() -> Path:
    return get_workspace_paths().project_dir / "permissions.saved.json"


def _audit_log_path() -> Path:
    return get_workspace_paths().project_dir / "permissions.audit.jsonl"


@dataclass(frozen=True)
class SavedPermissionRule:
    tool: str
    resource: str
    effect: PermissionEffect = "allow"
    scope: str = "session"
    created_at: int = 0


_session_rules: dict[str, list[SavedPermissionRule]] = {}


def _session_key() -> str:
    return str(get_workspace_paths().root.resolve())


def _now() -> int:
    return int(time.time())


def _normalize_entry(raw: object, *, default_scope: str) -> SavedPermissionRule | None:
    if not isinstance(raw, dict):
        return None
    tool = str(raw.get("tool") or "").strip()
    resource = str(raw.get("resource") or "").strip()
    effect = str(raw.get("effect") or "allow").strip()
    if not tool or not resource or effect not in ("allow", "ask", "deny"):
        return None
    return SavedPermissionRule(
        tool=tool,
        resource=resource,
        effect=effect,  # type: ignore[arg-type]
        scope=str(raw.get("scope") or default_scope),
        created_at=int(raw.get("created_at") or 0),
    )


def session_rules() -> list[SavedPermissionRule]:
    return list(_session_rules.get(_session_key(), ()))


def clear_session_rules() -> None:
    _session_rules.pop(_session_key(), None)


def add_session_rule(tool: str, resource: str, effect: PermissionEffect = "allow") -> SavedPermissionRule:
    rule = SavedPermissionRule(
        tool=tool,
        resource=resource,
        effect=effect,
        scope="session",
        created_at=_now(),
    )
    _session_rules.setdefault(_session_key(), []).append(rule)
    return rule


def load_persistent_rules() -> list[SavedPermissionRule]:
    path = _saved_rules_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rules: list[SavedPermissionRule] = []
    for item in items:
        rule = _normalize_entry(item, default_scope="persistent")
        if rule is not None:
            rules.append(rule)
    return rules


def save_persistent_rules(rules: list[SavedPermissionRule]) -> None:
    path = _saved_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"rules": [asdict(rule) for rule in rules]}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def add_persistent_rule(tool: str, resource: str, effect: PermissionEffect = "allow") -> SavedPermissionRule:
    rules = load_persistent_rules()
    rule = SavedPermissionRule(
        tool=tool,
        resource=resource,
        effect=effect,
        scope="persistent",
        created_at=_now(),
    )
    rules.append(rule)
    save_persistent_rules(rules)
    return rule


def audit_permission(event: dict) -> None:
    path = _audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("ts", _now())
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
