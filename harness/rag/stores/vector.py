"""Chroma-backed vector store for chunk embeddings."""

from __future__ import annotations

import threading
from typing import Any

from harness.rag.config import COLLECTION_NAME
from harness.settings import workspace_generation

_lock = threading.Lock()
_store: VectorStore | None = None
_store_generation = -1


class VectorStore:
    def __init__(self, persist_dir=None, collection_name: str = COLLECTION_NAME) -> None:
        from harness.rag.config import CHROMA_DIR

        self.persist_dir = persist_dir or CHROMA_DIR
        self.collection_name = collection_name
        self._client = None
        self._collection = None

    def _ensure_client(self):
        if self._client is not None:
            return self._collection
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError(
                "Vector store requires chromadb. Install: pip install chromadb"
            ) from exc
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def reset(self) -> None:
        collection = self._ensure_client()
        existing = collection.get(include=[])
        if existing and existing.get("ids"):
            collection.delete(ids=existing["ids"])

    def upsert_chunks(
        self,
        chunks: list[dict],
        embeddings: list[list[float]],
    ) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        if not chunks:
            return 0
        collection = self._ensure_client()
        ids = [chunk["id"] for chunk in chunks]
        documents = [chunk["text"] for chunk in chunks]
        metadatas = [_chunk_metadata(chunk) for chunk in chunks]
        batch_size = 100
        for start in range(0, len(chunks), batch_size):
            end = start + batch_size
            collection.upsert(
                ids=ids[start:end],
                documents=documents[start:end],
                embeddings=embeddings[start:end],
                metadatas=metadatas[start:end],
            )
        return len(chunks)

    def delete_sources(self, sources: set[str]) -> None:
        if not sources:
            return
        collection = self._ensure_client()
        for source in sources:
            try:
                collection.delete(where={"source": source})
            except Exception:
                continue

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 8,
        source: str | None = None,
        sources: list[str] | None = None,
        chapter: str | None = None,
        include_captions: bool = True,
    ) -> list[dict]:
        collection = self._ensure_client()
        where = _build_where(
            source=source,
            sources=sources,
            chapter=chapter,
            include_captions=include_captions,
        )
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": max(top_k, 1),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        result = collection.query(**kwargs)
        hits: list[dict] = []
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for chunk_id, text, meta, distance in zip(ids, documents, metadatas, distances):
            score = max(0.0, 1.0 - float(distance))
            hits.append(
                {
                    "id": chunk_id,
                    "text": text,
                    "score": round(score, 4),
                    "source": meta.get("source", ""),
                    "heading_path": meta.get("heading_path", ""),
                    "chapter": meta.get("chapter", ""),
                    "section": meta.get("section", ""),
                    "style": meta.get("style", ""),
                    "char_count": int(meta.get("char_count", 0)),
                    "is_caption": bool(meta.get("is_caption")),
                    "level": meta.get("level", "child"),
                    "parent_id": meta.get("parent_id") or None,
                    "child_index": int(meta.get("child_index", 0)),
                    "modality": meta.get("modality", "text"),
                    "asset_uri": meta.get("asset_uri", ""),
                    "page": int(meta.get("page", 0)),
                    "retrieval": "vector",
                }
            )
        return hits

    def count(self) -> int:
        collection = self._ensure_client()
        return collection.count()


def _chunk_metadata(chunk: dict) -> dict:
    return {
        "source": chunk.get("source", ""),
        "heading_path": chunk.get("heading_path", ""),
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "style": chunk.get("style", ""),
        "char_count": int(chunk.get("char_count", 0)),
        "is_caption": bool(chunk.get("is_caption")),
        "level": chunk.get("level", "child"),
        "parent_id": chunk.get("parent_id") or "",
        "child_index": int(chunk.get("child_index", 0)),
        "modality": chunk.get("modality", "text"),
        "asset_uri": chunk.get("asset_uri", ""),
        "page": int(chunk.get("page", 0)),
    }


def _build_where(
    *,
    source: str | None,
    sources: list[str] | None = None,
    chapter: str | None,
    include_captions: bool,
) -> dict | None:
    clauses: list[dict] = []
    allowed = list(sources or [])
    if source:
        allowed = [source]
    if allowed:
        if len(allowed) == 1:
            clauses.append({"source": allowed[0]})
        else:
            clauses.append({"source": {"$in": allowed}})
    if chapter:
        clauses.append({"chapter": chapter})
    if not include_captions:
        clauses.append({"is_caption": False})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def get_vector_store() -> VectorStore:
    global _store, _store_generation
    generation = workspace_generation()
    with _lock:
        if _store is None or _store_generation != generation:
            _store = VectorStore()
            _store_generation = generation
        return _store


def reset_vector_store() -> None:
    global _store, _store_generation
    with _lock:
        _store = None
        _store_generation = -1
