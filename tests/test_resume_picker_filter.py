"""Regression: /resume picker must show real sessions, not junk empty shells.

The picker previously called ``visible_session_summaries(limit=20)`` which
filtered AFTER truncating to 60 newest rows.  Hundreds of empty shell
directories (meta only, no session.jsonl, fresh updated_at) pushed every
real conversation out of the visible list, so `/resume` could only ever
offer the current empty shell.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness.project.session_registry as sr
from harness.project.session_registry import (
    create_session,
    list_session_summaries,
    visible_session_summaries,
)


def _make_shell(sessions_dir: Path, sid: str, *, updated_at: int) -> None:
    """A shell session dir: meta only, no session.jsonl (what broken backends leave)."""
    root = sessions_dir / sid
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.meta.json").write_text(
        json.dumps(
            {
                "active_persisted": 0,
                "title": "(untitled)",
                "created_at": updated_at,
                "updated_at": updated_at,
            }
        ),
        encoding="utf-8",
    )


def _make_real(sessions_dir: Path, sid: str, *, updated_at: int, title: str, messages: list) -> None:
    root = sessions_dir / sid
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.meta.json").write_text(
        json.dumps(
            {
                "active_persisted": len(messages),
                "title": title,
                "created_at": updated_at,
                "updated_at": updated_at,
            }
        ),
        encoding="utf-8",
    )
    lines = "\n".join(
        json.dumps({"type": "message", "role": m["role"], "content": m["content"]}, ensure_ascii=False)
        for m in messages
    )
    (root / "session.jsonl").write_text(lines + "\n", encoding="utf-8")


class TestResumePickerFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        project = self._root / ".project"
        project.mkdir()
        self._sessions = project / "sessions"
        self._sessions.mkdir()
        self._patcher = mock.patch.object(
            sr, "PROJECT_DIR", project
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_real_sessions_beat_newest_shells(self) -> None:
        """Even when 100 fresh empty shells exist, real conversations show first."""
        now = int(time.time())
        # 100 empty shells created *after* the real conversations
        for i in range(100):
            _make_shell(self._sessions, f"shell_{i:03d}", updated_at=now - 100 + i)
        # Real sessions are older, but have messages and titles
        real_ids = []
        for i in range(5):
            sid = f"real_{i}"
            _make_real(
                self._sessions,
                sid,
                updated_at=now - 1000 - i,
                title=f"历史对话 {i}",
                messages=[{"role": "user", "content": f"问题 {i}"}, {"role": "assistant", "content": "回答"}],
            )
            real_ids.append(sid)

        rows = visible_session_summaries(limit=20)
        visible_ids = [r["id"] for r in rows]
        # Every real session must be visible
        for sid in real_ids:
            self.assertIn(sid, visible_ids, f"real session {sid} squeezed out of picker")
        # At most 4 shell sessions may appear (limit 20, after the 5 real ones)
        shell_count = sum(1 for sid in visible_ids if sid.startswith("shell_"))
        self.assertLessEqual(shell_count, 20 - len(real_ids))

    def test_hide_empty_keeps_active_shell(self) -> None:
        """hide_empty must keep the current active shell (it is the live session)."""
        binding = create_session()
        active_id = binding.session_id
        rows = visible_session_summaries(limit=10, hide_empty=True)
        ids = [r["id"] for r in rows]
        self.assertIn(active_id, ids)

    def test_list_summaries_respects_limit_after_filter(self) -> None:
        """list_session_summaries limit is applied to visible rows, not raw newest."""
        now = int(time.time())
        for i in range(70):
            _make_shell(self._sessions, f"shell_{i:03d}", updated_at=now - 50 + i)
        for i in range(3):
            _make_real(
                self._sessions,
                f"real_{i}",
                updated_at=now - 500 - i,
                title=f"会话 {i}",
                messages=[{"role": "user", "content": "hi"}],
            )
        rows = list_session_summaries(limit=20)
        ids = [r["id"] for r in rows]
        self.assertIn("real_0", ids)
        self.assertIn("real_1", ids)


if __name__ == "__main__":
    unittest.main()