"""Persist which indexed sources are active for file-mode /rag ask."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.rag import config as rag_config
from harness.rag.ingest import load_manifest


# Backward-compatible fixture seams; production resolution remains dynamic.
SELECTION_PATH: Path | None = None
RAG_DIR: Path | None = None


def _selection_path() -> Path:
    override = globals().get("SELECTION_PATH")
    return (
        Path(override)
        if override is not None
        else Path(rag_config.RAG_DIR / "selection.json")
    )


def _rag_dir() -> Path:
    override = globals().get("RAG_DIR")
    return Path(override) if override is not None else Path(rag_config.RAG_DIR)

SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
SCOPE_UNSET = "unset"


def _read_payload() -> dict:
    path = _selection_path()
    if not path.exists():
        legacy = rag_config.legacy_path("RAG_DIR") / "selection.json"
        if not legacy.exists():
            return {}
        path = legacy
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_payload(payload: dict) -> None:
    path = _selection_path()
    _rag_dir().mkdir(parents=True, exist_ok=True)
    payload = {
        **payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_selection() -> list[str]:
    data = _read_payload()
    sources = data.get("sources") or []
    return [str(item) for item in sources if item]


def get_scope() -> str:
    data = _read_payload()
    scope = str(data.get("scope") or SCOPE_UNSET).strip().lower()
    if scope in (SCOPE_ALL, SCOPE_SELECTED, SCOPE_UNSET):
        return scope
    # Legacy: sources present → selected; empty → treat as all for ask
    if load_selection():
        return SCOPE_SELECTED
    return SCOPE_UNSET


def set_scope(scope: str) -> str:
    scope = scope.strip().lower()
    if scope not in (SCOPE_ALL, SCOPE_SELECTED, SCOPE_UNSET):
        raise ValueError(f"Unknown scope: {scope}")
    data = _read_payload()
    sources = data.get("sources") or []
    if scope == SCOPE_ALL:
        sources = []
    _write_payload({"scope": scope, "sources": list(sources)})
    return scope


def save_selection(sources: list[str]) -> None:
    _write_payload(
        {
            "scope": SCOPE_SELECTED if sources else SCOPE_ALL,
            "sources": sources,
        }
    )


def clear_selection() -> None:
    _write_payload({"scope": SCOPE_ALL, "sources": []})


def set_selection(sources: list[str]) -> list[str]:
    manifest = load_manifest()
    known = set((manifest.get("sources") or {}).keys())
    cleaned: list[str] = []
    for source in sources:
        if source in known and source not in cleaned:
            cleaned.append(source)
    save_selection(cleaned)
    return cleaned


def get_active_sources() -> list[str] | None:
    """Return selected sources, or None meaning search all indexed docs."""
    scope = get_scope()
    if scope == SCOPE_ALL or scope == SCOPE_UNSET:
        return None
    selected = load_selection()
    if not selected:
        return None
    manifest = load_manifest()
    known = set((manifest.get("sources") or {}).keys())
    active = [source for source in selected if source in known]
    return active or None


def format_selection_summary() -> str:
    scope = get_scope()
    if scope == SCOPE_ALL or (scope == SCOPE_UNSET and not load_selection()):
        return "检索范围：全部已索引文档"
    active = load_selection()
    if not active:
        return "检索范围：指定文档（列表为空 — 请 /rag pick）"
    lines = ["检索范围：指定文档"]
    for source in active:
        lines.append(f"  - {source}")
    return "\n".join(lines)
