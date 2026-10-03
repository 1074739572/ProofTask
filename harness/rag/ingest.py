"""Parse corpus files and write chunk JSONL artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.rag.assets import remove_source_assets, reset_source_assets
from harness.rag.chunking import chunk_docx, chunk_markdown, chunk_pdf, merge_short_chunk_dicts
from harness.rag import config as rag_config
from harness.rag.config import DEFAULT_CORPUS, SUPPORTED_SUFFIXES
from harness.rag.enrichment import (
    configured_vlm_model,
    pdf_ocr_mode,
    reset_vision_stats,
    vision_stats,
)
from harness.rag.parents import is_parent, is_searchable
from harness.settings import PACKAGE_ROOT, get_workdir

# Current workspace root as a module attribute so tests can monkeypatch it and
# workspace switching can re-point it without restarting the process.
WORKDIR = get_workdir()
_INITIAL_WORKDIR = WORKDIR


def _workdir() -> Path:
    # Keep explicit test overrides, but follow the active workspace in production.
    return WORKDIR if WORKDIR != _INITIAL_WORKDIR else get_workdir()


def resolve_path(path: str | None) -> Path:
    if not path:
        candidate = DEFAULT_CORPUS
        if candidate.exists():
            return candidate
        return _workdir() / "files" / "样例"

    raw = Path(path)
    if raw.is_absolute():
        return raw
    for base in (_workdir(), PACKAGE_ROOT):
        candidate = (base / raw).resolve()
        if candidate.exists():
            return candidate
    return (_workdir() / raw).resolve()


def discover_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_SUFFIXES else []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(path)
    return files


def _source_label(path: Path, root: Path) -> str:
    if root.is_file():
        return root.name
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def parse_file(path: Path, source: str) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
        raw = [chunk.to_dict() for chunk in chunk_markdown(source, text)]
        return merge_short_chunk_dicts(raw)
    if suffix == ".docx":
        raw = [chunk.to_dict() for chunk in chunk_docx(source, path)]
        return merge_short_chunk_dicts(raw)
    if suffix == ".pdf":
        asset_dir = reset_source_assets(source)
        raw = [chunk.to_dict() for chunk in chunk_pdf(source, path, asset_dir=asset_dir)]
        return merge_short_chunk_dicts(raw)
    return []


def _paths() -> tuple[Path, Path, Path, Path]:
    """Resolve active paths at call time; tolerate legacy test overrides."""
    return (
        Path(globals().get("CHUNKS_DIR", rag_config.CHUNKS_DIR)),
        Path(globals().get("RAG_DIR", rag_config.RAG_DIR)),
        Path(globals().get("MANIFEST_PATH", rag_config.MANIFEST_PATH)),
        Path(rag_config.legacy_path("CHUNKS_DIR")),
    )


def _read_chunks(path: Path) -> list[dict]:
    if not path.exists():
        return []
    chunks = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def load_chunks_for_source(source: str) -> list[dict]:
    rag_config.ensure_legacy_migrated()
    chunks_dir, _, _, legacy_dir = _paths()
    safe_name = source.replace("/", "__").replace("\\", "__")
    filename = f"{safe_name}.chunks.jsonl"

    path = chunks_dir / filename
    if path.exists():
        return _read_chunks(path)

    return _read_chunks(legacy_dir / filename)


def write_chunks_jsonl(source: str, chunks: list[dict]) -> Path:
    chunks_dir, _, _, _ = _paths()
    chunks_dir.mkdir(parents=True, exist_ok=True)

    safe_name = source.replace("/", "__").replace("\\", "__")
    out = chunks_dir / f"{safe_name}.chunks.jsonl"

    with out.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return out


def remove_chunks_for_source(source: str) -> None:
    chunks_dir, _, _, _ = _paths()
    safe_name = source.replace("/", "__").replace("\\", "__")
    (chunks_dir / f"{safe_name}.chunks.jsonl").unlink(missing_ok=True)


def load_manifest() -> dict:
    rag_config.ensure_legacy_migrated()
    _, _, manifest_path, _ = _paths()

    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    legacy = rag_config.legacy_path("MANIFEST_PATH")
    if legacy.exists():
        return json.loads(legacy.read_text(encoding="utf-8"))

    return {"schema_version": 2, "sources": {}, "embedding_model": None}


def save_manifest(manifest: dict) -> None:
    _, rag_dir, manifest_path, _ = _paths()
    rag_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def ingest_path(path: str | None = None) -> dict:
    root = resolve_path(path)
    if not root.exists():
        raise FileNotFoundError(f"Corpus path not found: {root}")

    files = discover_files(root)
    if not files:
        raise FileNotFoundError(f"No supported files (.md/.txt/.docx/.pdf) under: {root}")

    reset_vision_stats()
    manifest = load_manifest()
    manifest["schema_version"] = 2
    manifest["vision_model"] = configured_vlm_model()
    manifest["pdf_ocr_mode"] = pdf_ocr_mode()
    manifest.setdefault("sources", {})
    indexed = []
    errors = []
    source_labels = {_source_label(file_path, root) for file_path in files}
    previous_root = manifest.get("corpus_root")
    current_root = str(root.resolve())
    stale_sources = set(manifest["sources"])
    if previous_root == current_root:
        stale_sources -= source_labels
    for source in stale_sources:
        manifest["sources"].pop(source, None)
        remove_chunks_for_source(source)
        remove_source_assets(source)

    for file_path in files:
        source = _source_label(file_path, root)
        try:
            stat = file_path.stat()
            chunks = parse_file(file_path, source)
        except Exception as exc:
            errors.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})
            continue
        write_chunks_jsonl(source, chunks)
        parent_count = sum(1 for chunk in chunks if is_parent(chunk))
        child_count = sum(1 for chunk in chunks if is_searchable(chunk))
        modality_counts: dict[str, int] = {}
        for chunk in chunks:
            modality = chunk.get("modality", "text")
            modality_counts[modality] = modality_counts.get(modality, 0) + 1
        manifest["sources"][source] = {
            "path": str(file_path),
            "mtime": stat.st_mtime,
            "chars": sum(len(c["text"]) for c in chunks),
            "chunks": len(chunks),
            "parent_chunks": parent_count,
            "child_chunks": child_count,
            "suffix": file_path.suffix.lower(),
            "modalities": modality_counts,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        indexed.append(
            {
                "source": source,
                "chunks": len(chunks),
                "parent_chunks": parent_count,
                "child_chunks": child_count,
                "chars": sum(len(c["text"]) for c in chunks),
                "modalities": modality_counts,
            }
        )

    manifest["corpus_root"] = current_root
    manifest["indexed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["vision_stats"] = vision_stats()
    save_manifest(manifest)
    return {
        "root": str(root),
        "files": indexed,
        "errors": errors,
        "total_chunks": sum(f["chunks"] for f in indexed),
    }
