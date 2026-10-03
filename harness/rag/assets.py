"""Persistent storage for extracted RAG document assets."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from harness.rag import config as rag_config


# Backward-compatible fixture seam; production resolution remains dynamic.
ASSETS_DIR: Path | None = None


def _assets_dir() -> Path:
    override = globals().get("ASSETS_DIR")
    return Path(override) if override is not None else Path(rag_config.ASSETS_DIR)


def source_asset_dir(source: str) -> Path:
    """Return the stable, filesystem-safe asset directory for one source."""
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    return _assets_dir() / digest


def reset_source_assets(source: str) -> Path:
    """Remove stale extracted assets before re-ingesting a source."""
    directory = source_asset_dir(source)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def remove_source_assets(source: str) -> None:
    """Delete extracted assets for a source removed from the corpus."""
    directory = source_asset_dir(source)
    if directory.exists():
        shutil.rmtree(directory)


def save_asset(
    source: str,
    *,
    name: str,
    payload: bytes,
    asset_dir: Path | None = None,
) -> str:
    """Store bytes under ``.rag/assets`` and return a portable relative URI."""
    directory = asset_dir or source_asset_dir(source)
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = Path(name).name
    target = directory / safe_name
    target.write_bytes(payload)
    return target.relative_to(_assets_dir().parent).as_posix()
