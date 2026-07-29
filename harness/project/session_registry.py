"""Session registry: one directory per conversation, active pointer.

Layout (Claude Code / OpenCode style — conversation ≠ long workflow)::

    .project/
      active_session.json          # {"id": "<session-id>"}
      sessions/
        <id>/
          session.jsonl            # messages
          session.meta.json        # persistence cursor + title / timestamps
          todos.json               # A: session-scoped task list
      state.json                   # B: long-running workflow (thesis), single slot

Legacy flat files (``.project/session.jsonl``, ``todos.json``, ``session.meta.json``)
are migrated once into ``sessions/<id>/``.

**Per-process binding** — once a CLI window starts, its writes are pinned to
a single ``session_id``.  Another window creating or switching sessions only
updates ``active_session.json`` (the *suggestion* for the next launch), never
the writes of an already-running window.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.settings import PROJECT_DIR

SESSIONS_DIR = PROJECT_DIR / "sessions"
ACTIVE_SESSION_PATH = PROJECT_DIR / "active_session.json"

# Legacy flat layout (pre session-scoped storage)
LEGACY_SESSION_PATH = PROJECT_DIR / "session.jsonl"
LEGACY_SESSION_META_PATH = PROJECT_DIR / "session.meta.json"
LEGACY_TODOS_PATH = PROJECT_DIR / "todos.json"


@dataclass(frozen=True)
class SessionPaths:
    session_id: str
    root: Path

    @property
    def session_jsonl(self) -> Path:
        return self.root / "session.jsonl"

    @property
    def meta_json(self) -> Path:
        return self.root / "session.meta.json"

    @property
    def todos_json(self) -> Path:
        return self.root / "todos.json"


@dataclass(frozen=True)
class SessionBinding:
    """Per-process session handle — all writes target this id, never re-reads active_session.json."""
    session_id: str
    paths: SessionPaths

    @property
    def session_jsonl(self) -> Path:
        return self.paths.session_jsonl

    @property
    def meta_json(self) -> Path:
        return self.paths.meta_json

    @property
    def todos_json(self) -> Path:
        return self.paths.todos_json


def sessions_root() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def _new_session_id() -> str:
    # Sortable-ish: time prefix + short uuid for uniqueness
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"


def read_active_session_id() -> str | None:
    if not ACTIVE_SESSION_PATH.exists():
        return None
    try:
        data = json.loads(ACTIVE_SESSION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    sid = data.get("id")
    return str(sid) if sid else None


def write_active_session_id(session_id: str) -> None:
    """Update the global active pointer — only at session create / explicit /resume switch."""
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_SESSION_PATH.write_text(
        json.dumps({"id": session_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def session_paths(session_id: str | None = None) -> SessionPaths:
    sid = session_id or read_active_session_id()
    if not sid:
        raise RuntimeError("No active session id — call ensure_active_session() first")
    root = sessions_root() / sid
    root.mkdir(parents=True, exist_ok=True)
    return SessionPaths(session_id=sid, root=root)


def session_binding(session_id: str) -> SessionBinding:
    """Create a binding for an existing or new session directory."""
    root = sessions_root() / session_id
    root.mkdir(parents=True, exist_ok=True)
    return SessionBinding(
        session_id=session_id,
        paths=SessionPaths(session_id=session_id, root=root),
    )


def _default_meta(*, title: str = "", created_at: int | None = None) -> dict:
    now = int(time.time()) if created_at is None else created_at
    return {
        "active_persisted": 0,
        "title": title or "(untitled)",
        "created_at": now,
        "updated_at": now,
    }


def read_session_meta(binding: SessionBinding | None = None, paths: SessionPaths | None = None) -> dict:
    target = paths or (binding.paths if binding else session_paths())
    if not target.meta_json.exists():
        return _default_meta()
    try:
        data = json.loads(target.meta_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_meta()
    if "created_at" not in data:
        data["created_at"] = int(time.time())
    if "updated_at" not in data:
        data["updated_at"] = data["created_at"]
    if "title" not in data:
        data["title"] = "(untitled)"
    if "active_persisted" not in data:
        data["active_persisted"] = 0
    return data


def write_session_meta(meta: dict, binding: SessionBinding | None = None, paths: SessionPaths | None = None) -> None:
    target = paths or (binding.paths if binding else session_paths())
    target.root.mkdir(parents=True, exist_ok=True)
    meta = dict(meta)
    meta["updated_at"] = int(time.time())
    target.meta_json.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def touch_session_title_from_query(query: str, *, binding: SessionBinding) -> None:
    """Set title from first user query if still untitled."""
    meta = read_session_meta(binding=binding)
    title = (meta.get("title") or "").strip()
    if title and title != "(untitled)":
        return
    text = " ".join((query or "").strip().split())
    if not text:
        return
    if text.startswith("/"):
        return
    if len(text) > 48:
        text = text[:47] + "…"
    meta["title"] = text
    write_session_meta(meta, binding=binding)


def create_session(*, title: str = "") -> SessionBinding:
    sid = _new_session_id()
    binding = session_binding(sid)
    write_session_meta(_default_meta(title=title or "(untitled)"), binding=binding)
    write_active_session_id(sid)
    return binding


def migrate_legacy_flat_session() -> SessionBinding | None:
    """Move flat ``.project/session.jsonl`` (+ meta/todos) into ``sessions/<id>/``."""
    if not LEGACY_SESSION_PATH.exists() and not LEGACY_TODOS_PATH.exists():
        return None
    # Prefer existing active id if already pointing at a real dir
    existing = read_active_session_id()
    if existing and (sessions_root() / existing).is_dir():
        if (sessions_root() / existing / "session.jsonl").exists():
            return session_binding(existing)

    sid = _new_session_id()
    binding = session_binding(sid)

    if LEGACY_SESSION_PATH.exists():
        LEGACY_SESSION_PATH.rename(binding.session_jsonl)
    if LEGACY_SESSION_META_PATH.exists():
        LEGACY_SESSION_META_PATH.rename(binding.meta_json)
    else:
        write_session_meta(_default_meta(title="(migrated)"), binding=binding)
    if LEGACY_TODOS_PATH.exists():
        LEGACY_TODOS_PATH.rename(binding.todos_json)

    # Refresh meta timestamps / title defaults
    meta = read_session_meta(binding=binding)
    if not meta.get("title") or meta.get("title") == "(untitled)":
        meta["title"] = "(migrated)"
    write_session_meta(meta, binding=binding)
    write_active_session_id(sid)
    return binding


def ensure_active_session(*, fresh: bool) -> SessionBinding:
    """Ensure an active session directory exists.

    ``fresh=True`` (OpenCode default): always create a new session id so todos
    do not leak across launches. Legacy flat files are migrated into an
    archived session first (not reused as active).

    ``fresh=False`` (``HARNESS_CONTINUE_SESSION=1``): reuse active session, or
    migrate legacy flat layout into the active session.
    """
    sessions_root()
    migrated = migrate_legacy_flat_session()

    if fresh:
        # Migrated legacy becomes a past session; start a clean active one.
        return create_session()

    sid = read_active_session_id()
    if sid and (sessions_root() / sid).is_dir():
        return session_binding(sid)
    if migrated is not None:
        return migrated
    return create_session()


def list_session_summaries(*, limit: int = 20) -> list[dict]:
    """Newest-first summaries for /resume status (not a full picker yet)."""
    if not SESSIONS_DIR.exists():
        return []
    active = read_active_session_id()
    rows: list[dict] = []
    for path in SESSIONS_DIR.iterdir():
        if not path.is_dir():
            continue
        sp = SessionPaths(session_id=path.name, root=path)
        meta = read_session_meta(paths=sp)
        msg_count = 0
        if sp.session_jsonl.exists():
            try:
                for line in sp.session_jsonl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "message":
                        msg_count += 1
            except OSError:
                msg_count = 0
        rows.append(
            {
                "id": path.name,
                "title": meta.get("title") or "(untitled)",
                "created_at": int(meta.get("created_at") or 0),
                "updated_at": int(meta.get("updated_at") or 0),
                "messages": msg_count,
                "active": path.name == active,
                "has_todos": sp.todos_json.exists(),
            }
        )
    rows.sort(
        key=lambda r: (r["updated_at"] or r["created_at"], r["id"]),
        reverse=True,
    )
    return rows[:limit]


def visible_session_summaries(*, limit: int = 20, hide_empty: bool = True) -> list[dict]:
    """Same filter/order as the /resume list (1-based index maps to this)."""
    rows = list_session_summaries(limit=limit * 3)
    if hide_empty:
        rows = [
            r
            for r in rows
            if r["active"]
            or r["messages"] > 0
            or (r["title"] not in ("(untitled)", "(migrated)", "") and r["title"])
        ]
    return rows[:limit]


def format_session_list_block(*, limit: int = 20, hide_empty: bool = True) -> str:
    """Compact numbered list: title + created time."""
    rows = visible_session_summaries(limit=limit, hide_empty=hide_empty)
    if not rows:
        return "（暂无会话）\n用法：/resume <序号> 切换  ·  /resume delete <序号> 删除"
    lines = ["会话", "用法：/resume <序号> 切换  ·  /resume delete <序号> 删除"]
    for index, row in enumerate(rows, start=1):
        mark = "  ← 当前" if row["active"] else ""
        created = row["created_at"] or row["updated_at"]
        ts = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
            if created
            else "—"
        )
        title = row["title"] or "(untitled)"
        lines.append(f"  {index}. {title}  ·  {ts}{mark}")
    return "\n".join(lines)


def resolve_session_selector(selector: str) -> tuple[dict | None, str | None]:
    """Resolve ``2`` / title substring / session id → summary row."""
    text = (selector or "").strip()
    if not text:
        return None, "请指定序号，例如 /resume 2"
    rows = visible_session_summaries()
    if not rows:
        return None, "暂无会话可切换"

    if text.isdigit():
        index = int(text)
        if index < 1 or index > len(rows):
            return None, f"序号超出范围（1–{len(rows)}）"
        return rows[index - 1], None

    # Exact id
    for row in rows:
        if row["id"] == text:
            return row, None

    # Title substring (case-insensitive)
    low = text.lower()
    hits = [r for r in rows if low in (r["title"] or "").lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        titles = "、".join(h["title"] for h in hits[:5])
        return None, f"匹配到多个会话：{titles}。请用序号 /resume N"
    return None, f"未找到会话：{text!r}。先 /resume 看列表，再用 /resume 2"


def delete_session_by_id(session_id: str) -> tuple[bool, str]:
    """Remove ``sessions/<id>/`` from disk. Returns (ok, title or error)."""
    root = sessions_root() / session_id
    if not root.is_dir():
        return False, f"会话不存在：{session_id}"
    sp = SessionPaths(session_id=session_id, root=root)
    title = read_session_meta(paths=sp).get("title") or "(untitled)"
    shutil.rmtree(root)
    return True, title
