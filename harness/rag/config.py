"""RAG paths and default parameters."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.settings import PACKAGE_ROOT, get_workdir

_DEFAULT_PATHS = {
    "RAG_DIR", "CHUNKS_DIR", "ASSETS_DIR", "INDEX_DIR", "CHROMA_DIR", "MANIFEST_PATH",
}


def _rag_dir() -> Path:
    return get_workdir() / ".project" / ".rag"


def _legacy_rag_dir() -> Path:
    return get_workdir() / ".rag"


def _path_for(name: str) -> Path:
    rag = _rag_dir()
    return {
        "RAG_DIR": rag,
        "CHUNKS_DIR": rag / "chunks",
        "ASSETS_DIR": rag / "assets",
        "INDEX_DIR": rag / "index",
        "CHROMA_DIR": rag / "chroma",
        "MANIFEST_PATH": rag / "manifest.json",
    }[name]


def __getattr__(name: str):
    if name in _DEFAULT_PATHS:
        return _path_for(name)
    raise AttributeError(name)


def _publish_one(src: Path, dst: Path) -> bool:
    if dst.exists():
        return True
    staging = dst.parent / f".{dst.name}.{uuid.uuid4().hex}.staging"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, staging)
        if dst.exists():
            return True
        os.replace(staging, dst)
        return True
    except OSError:
        return False
    finally:
        staging.unlink(missing_ok=True)


def ensure_legacy_migrated() -> None:
    """Best-effort per-artifact migration; legacy data remains the read fallback."""
    legacy = _legacy_rag_dir()
    if not legacy.exists():
        return
    rag = _rag_dir()
    for relative in ("chunks", "assets", "index", "chroma"):
        source_dir = legacy / relative
        if not source_dir.is_dir():
            continue
        for src in source_dir.rglob("*"):
            if src.is_file():
                _publish_one(src, rag / relative / src.relative_to(source_dir))
    legacy_manifest = legacy / "manifest.json"
    unified_manifest = rag / "manifest.json"
    if legacy_manifest.exists() and not unified_manifest.exists():
        _publish_one(legacy_manifest, unified_manifest)


def legacy_path(name: str) -> Path:
    return _legacy_rag_dir() / {
        "RAG_DIR": "",
        "CHUNKS_DIR": "chunks",
        "ASSETS_DIR": "assets",
        "INDEX_DIR": "index",
        "CHROMA_DIR": "chroma",
        "MANIFEST_PATH": "manifest.json",
    }[name]


DEFAULT_CORPUS = PACKAGE_ROOT / "files" / "样例"

EMBEDDING_MODEL = "hash-bow-256"
COLLECTION_NAME = "corpus"
RETRIEVAL_SCHEMA_VERSION = 2

TARGET_CHUNK_CHARS = 600
MAX_CHUNK_CHARS = 1200
MAX_PARENT_CHARS = 8000
MIN_CHUNK_CHARS = 40
OVERLAP_CHARS = 100

SUPPORTED_SUFFIXES = {".md", ".txt", ".docx", ".pdf"}


@dataclass(frozen=True)
class RetrievalSettings:
    mode: str
    embedding: str
    rerank: bool
    fetch_k: int
    rrf_k: int


def retrieval_settings() -> RetrievalSettings:
    rerank_raw = os.getenv("HARNESS_RAG_RERANK", "1").strip().lower()
    fetch_raw = os.getenv("HARNESS_RAG_FETCH_K", "20").strip()
    rrf_raw = os.getenv("HARNESS_RAG_RRF_K", "60").strip()
    try:
        fetch_k = max(5, min(int(fetch_raw), 50))
    except ValueError:
        fetch_k = 20
    try:
        rrf_k = max(1, int(rrf_raw))
    except ValueError:
        rrf_k = 60
    return RetrievalSettings(
        mode=os.getenv("HARNESS_RAG_MODE", "hybrid").strip().lower(),
        embedding=os.getenv("HARNESS_RAG_EMBEDDING", "auto").strip().lower(),
        rerank=rerank_raw not in ("0", "false", "no"),
        fetch_k=fetch_k,
        rrf_k=rrf_k,
    )
