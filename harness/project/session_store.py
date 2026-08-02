"""Claude Code-style session persistence: append-only session.jsonl + compact boundaries.

OpenCode mode (default): every launch is a fresh chat unless explicitly opted in
via ``HARNESS_CONTINUE_SESSION=1`` (Claude Code ``-c`` style).

Messages and session-scoped todos live under::

    .project/sessions/<id>/session.jsonl
    .project/sessions/<id>/todos.json

Long-running workflow state remains ``.project/state.json`` (single slot, opt-in).

**Per-process binding** — once a window starts, all writes go to its pinned
``SessionBinding``.  ``active_session.json`` is only read at bootstrap and during
explicit ``/resume`` switches.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from harness.project.session import (
    HISTORY_PATH,
    deserialize_messages,
    serialize_messages,
)
from harness.project.session_registry import (
    SessionBinding,
    create_session,
    ensure_active_session,
    read_active_session_id,
    read_session_meta,
    session_binding,
    write_active_session_id,
    write_session_meta,
)
from harness.project import session_registry as session_registry
from harness.settings import PROJECT_DIR, TRANSCRIPT_DIR


def continue_session_on_startup() -> bool:
    """Whether ``bootstrap_session`` should reload the active session on restart.

    OpenCode-style default: False (every launch is a fresh chat, like ``claude``
    without ``-c``). Set ``HARNESS_CONTINUE_SESSION=1`` to get the Claude Code
    ``-c`` behavior back.
    """
    flag = os.getenv("HARNESS_CONTINUE_SESSION", "0").strip().lower()
    return flag in ("1", "true", "yes", "on")


def bootstrap_from_transcript_enabled() -> bool:
    """Whether an empty session may pull context from ``.transcripts/``.

    Default off (OpenCode). ``HARNESS_BOOTSTRAP_TRANSCRIPT=1`` restores the old
    fallback that rehydrated the latest transcript into a new session.
    """
    flag = os.getenv("HARNESS_BOOTSTRAP_TRANSCRIPT", "0").strip().lower()
    return flag in ("1", "true", "yes", "on")


# ── per-binding helpers ──────────────────────────────────────────────

def _load_meta(binding: SessionBinding) -> dict:
    return read_session_meta(binding=binding)


def _save_meta(meta: dict, binding: SessionBinding) -> None:
    write_session_meta(meta, binding=binding)


def _append_record(record: dict, binding: SessionBinding) -> None:
    path = binding.session_jsonl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_records(binding: SessionBinding) -> list[dict]:
    path = binding.session_jsonl
    if not path.exists():
        return []
    records: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid session JSONL line {line_no}") from exc
    return records


def _message_from_record(record: dict) -> dict | None:
    if record.get("type") != "message":
        return None
    role = record.get("role")
    if not role:
        return None
    return {"role": role, "content": record.get("content")}


def _active_start_index(records: list[dict]) -> int:
    last_boundary = -1
    for index, record in enumerate(records):
        if record.get("type") == "compact_boundary":
            last_boundary = index
    return last_boundary + 1


def _append_message_records(
    serialized_messages: list[dict],
    binding: SessionBinding,
    start: int = 0,
) -> int:
    appended = 0
    for message in serialized_messages[start:]:
        _append_record({"type": "message", **message}, binding)
        appended += 1
    return appended


# ── public API (all require explicit session_id or binding) ──────────

def load_session_messages(*, session_id: str | None = None, binding: SessionBinding | None = None) -> list | None:
    """Load active messages for *session_id* (or the given binding)."""
    b = binding or session_binding(session_id or read_active_session_id() or "?")
    records = _read_records(b)
    if not records:
        return None

    raw_messages: list[dict] = []
    for record in records[_active_start_index(records):]:
        message = _message_from_record(record)
        if message is not None:
            raw_messages.append(message)

    if not raw_messages:
        return None
    return deserialize_messages(raw_messages)


def session_stats(*, session_id: str | None = None, binding: SessionBinding | None = None) -> dict:
    if binding is not None:
        b = binding
    elif session_id is not None:
        b = session_binding(session_id)
    else:
        b = session_binding(read_active_session_id() or "?")
    path = b.session_jsonl
    records = _read_records(b) if path.exists() else []
    active_start = _active_start_index(records)
    active_messages = sum(
        1 for record in records[active_start:] if record.get("type") == "message"
    )
    boundaries = sum(1 for record in records if record.get("type") == "compact_boundary")
    return {
        "path": str(path),
        "session_id": b.session_id,
        "exists": path.exists(),
        "total_records": len(records),
        "active_messages": active_messages,
        "compact_boundaries": boundaries,
        "size_kb": path.stat().st_size // 1024 if path.exists() else 0,
    }


def append_checkpoint(messages: list, *, binding: SessionBinding) -> None:
    """Persist *messages* to the bound session.  Never reads active_session.json."""
    if not messages:
        return

    binding.paths.root.mkdir(parents=True, exist_ok=True)
    meta = _load_meta(binding)
    persisted = int(meta.get("active_persisted", 0))

    serialized = serialize_messages(messages)

    # Guard: if messages list shrank (e.g. compact), reset cursor.
    if persisted > len(serialized):
        persisted = 0

    _append_message_records(serialized, binding, start=persisted)
    # Use the filtered count as cursor so ephemeral messages don't cause drift.
    meta["active_persisted"] = len(serialized)
    _save_meta(meta, binding)


def replace_session(messages: list, *, binding: SessionBinding) -> None:
    clear_session(binding=binding, archive=True)
    if messages:
        append_checkpoint(messages, binding=binding)


def record_compact_boundary(
    mode: str,
    pre_tokens: int,
    transcript_path: str | Path,
    messages_after_compact: list,
    *,
    binding: SessionBinding,
) -> None:
    _append_record(
        {
            "type": "compact_boundary",
            "mode": mode,
            "pre_tokens": pre_tokens,
            "transcript": str(transcript_path).replace("\\", "/"),
            "ts": int(time.time()),
        },
        binding,
    )
    serialized = serialize_messages(messages_after_compact)
    _append_message_records(serialized, binding)
    meta = _load_meta(binding)
    meta["active_persisted"] = len(serialized)
    _save_meta(meta, binding)


def _migrate_history_json(binding: SessionBinding) -> list | None:
    if not HISTORY_PATH.exists():
        return None
    payload = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    raw = payload.get("messages", [])
    if not raw:
        return None

    for message in raw:
        _append_record({"type": "message", **message}, binding)
    meta = _load_meta(binding)
    meta["active_persisted"] = len(raw)
    meta["migrated_from"] = "history.json"
    _save_meta(meta, binding)
    return deserialize_messages(raw)


def _bootstrap_from_transcript(binding: SessionBinding) -> list | None:
    if not TRANSCRIPT_DIR.exists():
        return None
    transcripts = sorted(
        TRANSCRIPT_DIR.glob("transcript_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
    )
    if not transcripts:
        return None

    from harness.project.transcript import normalize_transcript_messages

    latest = transcripts[-1]
    try:
        raw = []
        for line in latest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                raw.append(json.loads(line))
        restored = normalize_transcript_messages(raw, mode="summary")
    except ValueError:
        return None

    serialized = serialize_messages(restored)
    for message in serialized:
        _append_record({"type": "message", **message}, binding)
    meta = _load_meta(binding)
    meta["active_persisted"] = len(serialized)
    meta["bootstrapped_from"] = latest.name
    _save_meta(meta, binding)
    return restored


def bootstrap_session() -> tuple[list, SessionBinding, str | None]:
    """OpenCode-style bootstrap. Returns (messages, binding, source_note).

    By default this returns an empty session on every launch (OpenCode mode) and
    creates a **new** ``sessions/<id>/`` so todos do not leak from prior chats.
    Enable ``HARNESS_CONTINUE_SESSION=1`` to reload the active session (Claude
    Code ``-c`` style) and optionally ``HARNESS_BOOTSTRAP_TRANSCRIPT=1`` to fall
    back to ``.transcripts/`` when no live session exists.

    The returned ``SessionBinding`` pins all subsequent writes for this process.
    """
    fresh = not continue_session_on_startup()
    binding = ensure_active_session(fresh=fresh)

    if fresh:
        return [], binding, None

    loaded = load_session_messages(binding=binding)
    if loaded:
        stats = session_stats(binding=binding)
        return (
            loaded,
            binding,
            f"sessions/{binding.session_id}/session.jsonl（{stats['active_messages']} 条活跃消息）",
        )

    migrated = _migrate_history_json(binding)
    if migrated:
        return migrated, binding, "已从 history.json 迁移至 session.jsonl"

    if bootstrap_from_transcript_enabled():
        from_transcript = _bootstrap_from_transcript(binding)
        if from_transcript:
            meta = _load_meta(binding)
            source = meta.get("bootstrapped_from", "transcript")
            return from_transcript, binding, f"从 .transcripts/{source} 引导恢复"

    return [], binding, None


def clear_session(*, binding: SessionBinding, archive: bool = True) -> str | None:
    """Archive the current session directory. Returns old path for display.

    Does NOT create a new session — the caller must do that via
    ``create_session()`` so the new binding is captured correctly.
    """
    old_id = binding.session_id
    old_path = str(binding.paths.root)

    if HISTORY_PATH.exists():
        HISTORY_PATH.unlink()

    # Drop legacy flat session if somehow still present
    legacy = session_registry.LEGACY_SESSION_PATH
    if legacy.exists():
        archived = PROJECT_DIR / f"session_{int(time.time())}.jsonl"
        legacy.rename(archived)

    if archive and old_path:
        return old_path
    return None


def format_session_line(*, binding: SessionBinding | None = None) -> str:
    stats = session_stats(binding=binding) if binding is not None else session_stats()
    sid = stats.get("session_id", "?")
    if not continue_session_on_startup():
        if stats["exists"] or sid not in ("?", ""):
            return (
                f"会话：OpenCode 模式（不自动续）。"
                f"当前 sessions/{sid}/"
                f"（{stats['active_messages']} 条）；"
                f"todos 跟本 session。"
                f"续对话：HARNESS_CONTINUE_SESSION=1；续论文：/resume project。"
            )
        return "会话：（空）OpenCode 模式 —— 每次启动都是全新对话（新 session + 空 todos）"
    if not stats["exists"]:
        return f"会话：sessions/{sid}/ （无消息）首条后开始写入"
    compact_part = ""
    if stats["compact_boundaries"]:
        compact_part = f"，{stats['compact_boundaries']} 次压缩"
    return (
        f"会话：sessions/{sid}/session.jsonl "
        f"（{stats['active_messages']} 条消息{compact_part}）"
    )


# Back-compat aliases for imports/tests that referenced flat paths
SESSION_PATH = session_registry.LEGACY_SESSION_PATH  # noqa: N816 — legacy name
