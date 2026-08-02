"""Auto-index local corpus when writing workflows start."""

from __future__ import annotations

from pathlib import Path

from harness.rag.enrichment import configured_vlm_model, pdf_ocr_mode
from harness.rag.config import RETRIEVAL_SCHEMA_VERSION
from harness.rag.ingest import (
    discover_files,
    ingest_path,
    load_manifest,
    resolve_path,
)
from harness.rag.pipeline import build_index, rag_status_dict
from harness.rag.tools import format_rag_index_result
from harness.settings import PACKAGE_ROOT, WORKDIR, get_workdir

DEFAULT_INDEX_PATH = "files"


def index_is_empty() -> bool:
    status = rag_status_dict()
    return not (status.get("sources") or {})


def corpus_root_exists(path: str = DEFAULT_INDEX_PATH) -> bool:
    try:
        root = resolve_path(path)
    except Exception:
        return False
    return root.exists()


def index_refresh_reason(path: str | None = None) -> str | None:
    """Return why the persisted snapshot is stale, or ``None`` when current."""
    manifest = load_manifest()
    sources = manifest.get("sources") or {}
    if not sources:
        return "索引为空"
    root_value = path or manifest.get("corpus_root")
    if not root_value:
        return "索引缺少语料目录信息"
    root = resolve_path(str(root_value))
    if not root.exists():
        return f"语料目录不存在: {root}"
    if str(root.resolve()) != str(Path(manifest.get("corpus_root", "")).resolve()):
        return "语料目录已切换"

    files = discover_files(root)
    current_paths = {str(file.resolve()): file for file in files}
    indexed_paths = {
        str(Path(meta.get("path", "")).resolve()): meta
        for meta in sources.values()
        if meta.get("path")
    }
    if set(current_paths) != set(indexed_paths):
        return "语料文件集合已变化"
    for file_path, file in current_paths.items():
        previous = float(indexed_paths[file_path].get("mtime", 0))
        if abs(file.stat().st_mtime - previous) > 1e-6:
            return f"文件已更新: {file.name}"
    if manifest.get("vision_model") != configured_vlm_model():
        return "视觉模型配置已变化"
    if manifest.get("pdf_ocr_mode", "off") != pdf_ocr_mode():
        return "PDF OCR 配置已变化"
    if manifest.get("retrieval_schema_version") != RETRIEVAL_SCHEMA_VERSION:
        return "检索策略版本已升级"
    return None


def ensure_rag_indexed(path: str = DEFAULT_INDEX_PATH) -> dict:
    """Build or refresh the local index when corpus exists.

    Returns a dict with keys: ok, action, message, path, chunks, sources.
    """
    try:
        root = resolve_path(path)
    except Exception as e:
        return {
            "ok": False,
            "action": "error",
            "message": str(e),
            "path": path,
            "chunks": 0,
            "sources": 0,
        }

    if not root.exists():
        return {
            "ok": False,
            "action": "missing",
            "message": (
                f"Corpus not found: {root}\n"
                "Put reference .md/.txt/.docx/.pdf under cwd/files/ "
                "(e.g. files/样例/, files/指标与交付要求.md), then retry."
            ),
            "path": str(root),
            "chunks": 0,
            "sources": 0,
        }

    was_empty = index_is_empty()
    if not was_empty:
        reason = index_refresh_reason(path)
        if reason is None:
            manifest = load_manifest()
            sources = manifest.get("sources") or {}
            return {
                "ok": True,
                "action": "unchanged",
                "message": (
                    f"RAG index is current: {root}\n"
                    f"Sources: {len(sources)}, chunks: {manifest.get('child_count', 0)}"
                ),
                "path": str(root),
                "chunks": manifest.get("child_count", 0),
                "sources": len(sources),
            }
    try:
        result = ingest_path(path)
        index_result = build_index([item["source"] for item in result["files"]])
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "action": "empty",
            "message": str(exc),
            "path": str(root),
            "chunks": 0,
            "sources": 0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "action": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "path": str(root),
            "chunks": 0,
            "sources": 0,
        }

    action = "indexed" if was_empty else "refreshed"
    return {
        "ok": True,
        "action": action,
        "message": format_rag_index_result(result, index_result),
        "path": str(root),
        "chunks": index_result.get("indexed_chunks", result["total_chunks"]),
        "sources": len(result["files"]),
    }


def bootstrap_message(result: dict) -> str:
    if not result.get("ok"):
        return (
            "[RAG bootstrap]\n"
            f"{result.get('message', 'index failed')}\n"
            "Writing mode needs files under cwd/files/. "
            "Add samples then run rag_index path=\"files\" or reload the task."
        )
    action = result.get("action", "indexed")
    return (
        f"[RAG bootstrap — {action}]\n"
        f"Corpus: {result.get('path')}\n"
        f"Sources: {result.get('sources', 0)}, chunks: {result.get('chunks', 0)}\n"
        "Use rag_search before each output/*.md section. "
        "Do not read_file whole reference documents; use rag_search instead."
    )


def suggested_corpus_dirs() -> list[Path]:
    workdir = get_workdir()
    candidates = [
        workdir / "files",
        PACKAGE_ROOT / "files",
        workdir / "files" / "样例",
        PACKAGE_ROOT / "files" / "样例",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            out.append(path)
    return out
