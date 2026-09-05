"""In-process workspace switching (opencode-style directory-bound sessions).

`/open <dir>` no longer restarts the backend process.  Instead it atomically:

1. flips the active workspace root in ``harness.settings`` (``get_workdir()``);
2. re-binds the session store to the new directory's ``.project/``;
3. refreshes RAG path constants + drops RAG in-memory caches;
4. resets the todos binding and reloads the new workspace's session.

File tools and the shell read ``get_workdir()`` at call time, so switching is
safe even while the agent loop is idle.  The whole operation is sub-second.
"""

from __future__ import annotations

import json
from pathlib import Path

# Recently opened workspaces, so `/open` with no argument can offer a picker.
# Stored under the user's home so it survives across processes and is shared
# by CLI + TUI.  Written on every successful switch; capped at 20 entries.
RECENT_PROJECTS_PATH = Path.home() / ".harness" / "recent_projects.json"
RECENT_PROJECTS_LIMIT = 20


def _read_recent_projects() -> list[str]:
    try:
        raw = RECENT_PROJECTS_PATH.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item) for item in data if str(item).strip()]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_recent_projects(projects: list[str]) -> None:
    try:
        RECENT_PROJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECENT_PROJECTS_PATH.write_text(
            json.dumps(projects, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort; a failed write must never break switching


def record_recent_project(root: Path) -> None:
    """Remember *root* as a recently opened workspace (newest first)."""
    try:
        resolved = str(root.expanduser().resolve())
    except OSError:
        return
    projects = [p for p in _read_recent_projects() if p != resolved]
    projects.insert(0, resolved)
    _write_recent_projects(projects[:RECENT_PROJECTS_LIMIT])


def list_recent_projects() -> list[dict]:
    """Return ``[{path, current}]`` newest-first for the `/open` picker."""
    current = str(get_workdir())
    return [
        {"path": project, "current": project == current}
        for project in _read_recent_projects()
    ]


def get_workdir() -> Path:
    from harness.settings import get_workdir as _get_workdir

    return _get_workdir()


def switch_workspace(target: str | Path) -> tuple[bool, str, object | None]:
    """Atomically switch the active workspace to *target*.

    Returns ``(ok, note, binding)``.  *binding* is the freshly bootstrapped
    session binding of the target workspace (None on failure).  On success the
    caller should rebuild the session context and emit UI events; the process
    keeps running.
    """
    root = Path(target).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        return False, f"Cannot open {str(target)!r}: {exc}", None
    if not resolved.is_dir():
        return False, f"Cannot open {str(target)!r}: not a directory", None

    from harness.settings import switch_workspace as _switch_root

    _switch_root(resolved)
    record_recent_project(resolved)
    _refresh_rag_paths()
    _reset_mcp_runtime()
    _reset_rag_caches()
    binding = _reset_session_state(resolved)

    return True, f"Switched workspace → {resolved}", binding


def _refresh_rag_paths() -> None:
    """Point every RAG module-level path constant at the new workspace.

    The RAG modules bind their directories at import time (``from ... import
    INDEX_DIR``), so a plain ``harness.rag.config`` update is not enough: we
    patch the already-imported module attributes here.
    """
    from harness import settings
    from harness.rag import config as rag_config

    root = settings.get_workdir()
    new_config = {
        "RAG_DIR": root / ".rag",
        "CHUNKS_DIR": root / ".rag" / "chunks",
        "ASSETS_DIR": root / ".rag" / "assets",
        "INDEX_DIR": root / ".rag" / "index",
        "CHROMA_DIR": root / ".rag" / "chroma",
        "MANIFEST_PATH": root / ".rag" / "manifest.json",
    }
    for name, value in new_config.items():
        setattr(rag_config, name, value)

    # Modules that re-export these constants into their own namespace.
    from harness.rag import assets, ingest, lexical, parents
    from harness.rag import selection

    setattr(lexical, "INDEX_DIR", new_config["INDEX_DIR"])
    setattr(lexical, "MANIFEST_PATH", new_config["MANIFEST_PATH"])
    setattr(lexical, "CORPUS_PATH", new_config["INDEX_DIR"] / "corpus.json")
    setattr(parents, "INDEX_DIR", new_config["INDEX_DIR"])
    setattr(parents, "PARENTS_PATH", new_config["INDEX_DIR"] / "parents.json")
    setattr(assets, "ASSETS_DIR", new_config["ASSETS_DIR"])
    setattr(ingest, "CHUNKS_DIR", new_config["CHUNKS_DIR"])
    setattr(ingest, "MANIFEST_PATH", new_config["MANIFEST_PATH"])
    setattr(ingest, "RAG_DIR", new_config["RAG_DIR"])
    # ingest.resolve_path() reads WORKDIR for default corpus resolution.
    setattr(ingest, "WORKDIR", root)
    # selection.py imports RAG_DIR by value, so point its persistence file at
    # the target workspace as well.
    setattr(selection, "SELECTION_PATH", new_config["RAG_DIR"] / "selection.json")


def _reset_rag_caches() -> None:
    """Drop RAG in-memory indexes so the next lookup rebuilds from the new dir."""
    from harness.rag import lexical, parents, pipeline

    lexical._corpus = []
    lexical._idf = {}
    lexical._doc_freq = {}
    lexical._avg_dl = 1.0
    parents._parent_map = {}
    pipeline.reset_runtime()


def _reset_mcp_runtime() -> None:
    """Close clients and invalidate bootstrap state after workspace changes."""
    from harness.mcp import pool

    for client in list(pool.mcp_clients.values()):
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    pool.mcp_clients.clear()
    pool.mcp_tool_meta.clear()
    pool.mcp_pool_warnings.clear()
    with pool._bootstrap_lock:
        pool._bootstrap_started = False
        pool._bootstrap_results.clear()
        pool._bootstrap_done.clear()
    with pool._barrier_lock:
        pool._barrier_consumed = False
        pool._warnings_consumed = False


def _reset_session_state(root: Path):
    """Re-bind todos and sessions to the new workspace.

    A fresh session is started in the target directory (opencode-style: every
    switch starts clean, matching ``bootstrap_session()`` defaults).  The old
    binding is discarded; nothing is deleted from disk.
    """
    from harness.project.session_store import bootstrap_session
    from harness.todos.state import clear_todos, set_binding

    set_binding(None)  # type: ignore[arg-type]  # None unpins the process binding
    clear_todos(delete_file=False)

    _messages, binding, _source = bootstrap_session()
    set_binding(binding)
    from harness.todos.state import load_todos_from_disk

    load_todos_from_disk(binding=binding)
    return binding
