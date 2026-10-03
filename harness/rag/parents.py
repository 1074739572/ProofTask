"""Parent chunk lookup and child-hit context expansion."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from harness.rag import config as rag_config
from harness.settings import workspace_generation


def _index_dir() -> Path:
    override = globals().get("INDEX_DIR")
    return Path(override) if override is not None else Path(rag_config.INDEX_DIR)


def _parents_path() -> Path:
    override = globals().get("PARENTS_PATH")
    return Path(override) if override is not None else _index_dir() / "parents.json"


# Backward-compatible fixture seam; production resolution remains dynamic.
PARENTS_PATH: Path | None = None
INDEX_DIR: Path | None = None


_lock = threading.Lock()
_parent_map: dict[str, dict] = {}
_parent_generation = -1


def is_parent(chunk: dict) -> bool:
    return chunk.get("level") == "parent"


def is_searchable(chunk: dict) -> bool:
    """Chunks indexed and searched: children, captions, and legacy flat chunks."""
    level = chunk.get("level")
    if level == "parent":
        return False
    return True


def split_chunks(chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    parents = [chunk for chunk in chunks if is_parent(chunk)]
    searchable = [chunk for chunk in chunks if is_searchable(chunk)]
    return parents, searchable


def build_parent_map(chunks: list[dict]) -> dict[str, dict]:
    return {chunk["id"]: chunk for chunk in chunks if is_parent(chunk)}


def persist_parents(parents: list[dict]) -> None:
    global _parent_map, _parent_generation
    generation = workspace_generation()
    _index_dir().mkdir(parents=True, exist_ok=True)
    merged = load_parent_map()
    merged.update(build_parent_map(parents))
    _parents_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with _lock:
        _parent_map = merged
        _parent_generation = generation


def replace_parents(parents: list[dict]) -> None:
    """Atomically replace parent context with the active index snapshot."""
    global _parent_map, _parent_generation
    generation = workspace_generation()
    payload = build_parent_map(parents)
    _index_dir().mkdir(parents=True, exist_ok=True)
    _parents_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with _lock:
        _parent_map = payload
        _parent_generation = generation


def remove_sources_from_parents(sources: set[str]) -> None:
    global _parent_map, _parent_generation
    generation = workspace_generation()
    if not sources:
        return
    merged = load_parent_map()
    merged = {
        chunk_id: chunk
        for chunk_id, chunk in merged.items()
        if chunk.get("source") not in sources
    }
    _index_dir().mkdir(parents=True, exist_ok=True)
    _parents_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with _lock:
        _parent_map = merged
        _parent_generation = generation


def load_parent_map() -> dict[str, dict]:
    global _parent_map, _parent_generation
    generation = workspace_generation()
    with _lock:
        if _parent_generation != generation:
            _parent_map = {}
            _parent_generation = generation
        if _parent_map:
            return dict(_parent_map)
    path = _parents_path()
    if not path.exists():
        legacy = rag_config.legacy_path("INDEX_DIR") / "parents.json"
        if not legacy.exists():
            return {}
        path = legacy
    data = json.loads(path.read_text(encoding="utf-8"))
    with _lock:
        _parent_map = data
        _parent_generation = generation
    return dict(data)


def reset_parent_cache() -> None:
    global _parent_map, _parent_generation
    with _lock:
        _parent_map = {}
        _parent_generation = -1


def _parent_char_limit() -> int:
    raw = os.getenv("HARNESS_RAG_PARENT_CHARS", "4000").strip()
    try:
        return max(800, int(raw))
    except ValueError:
        return 4000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[parent truncated]"


def attach_parent_context(hits: list[dict]) -> list[dict]:
    parents = load_parent_map()
    if not parents:
        return hits

    enriched: list[dict] = []
    for hit in hits:
        parent_id = hit.get("parent_id")
        if not parent_id:
            enriched.append(hit)
            continue
        parent = parents.get(parent_id)
        if not parent:
            enriched.append(hit)
            continue
        limit = _parent_char_limit()
        enriched.append(
            {
                **hit,
                "parent_id": parent_id,
                "parent_heading_path": parent.get("heading_path", ""),
                "parent_text": _truncate(parent.get("text", ""), limit),
                "parent_char_count": parent.get("char_count", 0),
            }
        )
    return enriched
