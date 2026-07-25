"""Tests for /mode file document Q&A routing."""

from pathlib import Path

import pytest

from harness.rag.commands import run_rag_index_command
from harness.rag.file_mode import handle_file_mode_turn, is_file_mode
from harness.rag.selection import (
    SCOPE_ALL,
    SCOPE_SELECTED,
    get_active_sources,
    get_scope,
    set_scope,
    set_selection,
)
from tests.rag_fixtures import rag_env  # noqa: F401

FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "rag" / "fixtures" / "tiny_corpus"
SAMPLE = FIXTURE / "sample_report.md"
METRICS = FIXTURE / "metrics.md"


@pytest.fixture()
def isolated_cwd(rag_env, tmp_path, monkeypatch):
    import harness.rag.ingest as ingest_mod
    import harness.settings as settings_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_mod, "WORKDIR", tmp_path)
    monkeypatch.setattr(ingest_mod, "WORKDIR", tmp_path)
    return tmp_path


@pytest.fixture()
def indexed_two_docs(isolated_cwd):
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "sample_report.md").write_text(
        SAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (corpus / "metrics.md").write_text(
        METRICS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    run_rag_index_command("files")
    return isolated_cwd


def test_modes_json_has_file():
    from harness.modes.registry import get_mode_profile, list_mode_ids

    assert "file" in list_mode_ids()
    profile = get_mode_profile("file")
    assert profile is not None
    assert "文档" in profile.summary or "file" in profile.label.lower()


def test_set_mode_file_enters_and_sets_scope(indexed_two_docs, monkeypatch):
    from harness.modes import get_mode, set_mode
    from harness.modes import runtime as runtime_mod
    from harness.rag.selection import SCOPE_UNSET, set_scope as _set_scope

    monkeypatch.setattr(runtime_mod, "_current_mode", "direct")
    _set_scope(SCOPE_UNSET)
    msg = set_mode("file")
    assert get_mode() == "file"
    assert "文件模式" in msg
    assert get_scope() == SCOPE_ALL
    # Must not require interactive picker after reset/clear
    assert "指定文档（多选）" not in msg


def test_file_mode_turn_always_rag(indexed_two_docs, monkeypatch):
    monkeypatch.setenv("HARNESS_RAG_QA_LLM", "0")
    set_scope(SCOPE_ALL)
    answer = handle_file_mode_turn("引言 编写目的是什么？")
    assert "检索到" in answer or "编写目的" in answer or "sample" in answer.lower()


def test_file_mode_scope_selected(indexed_two_docs, monkeypatch):
    monkeypatch.setenv("HARNESS_RAG_QA_LLM", "0")
    set_selection(["metrics.md"])
    assert get_scope() == SCOPE_SELECTED
    assert get_active_sources() == ["metrics.md"]
    answer = handle_file_mode_turn("性能指标 FY-4")
    assert "metrics.md" in answer


def test_file_mode_scope_intent_all(indexed_two_docs):
    set_selection(["sample_report.md"])
    msg = handle_file_mode_turn("搜全部")
    assert get_scope() == SCOPE_ALL
    assert "文件模式" in msg


def test_file_mode_list_docs(indexed_two_docs):
    text = handle_file_mode_turn("你现在有什么文档")
    assert "sample_report.md" in text
    assert "metrics.md" in text


def test_file_mode_empty_index_no_auto_reindex(isolated_cwd, monkeypatch):
    """After /rag reset, file mode must not silently rebuild index from files/."""
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "metrics.md").write_text(
        METRICS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    from harness.modes import set_mode

    msg = set_mode("file")
    assert "索引为空" in msg or "尚无已索引文档" in msg
    answer = handle_file_mode_turn("性能指标 FY-4")
    assert "索引为空" in answer or "/rag index" in answer
    assert not (isolated_cwd / ".rag" / "index" / "corpus.json").exists()


def test_file_mode_reports_stale_index(indexed_two_docs):
    metrics = indexed_two_docs / "files" / "metrics.md"
    metrics.write_text(metrics.read_text(encoding="utf-8") + "\n新增指标。", encoding="utf-8")
    answer = handle_file_mode_turn("性能指标是什么？")
    assert "索引已过期" in answer
    assert "/rag index files" in answer


def test_is_file_mode_flag(monkeypatch):
    from harness.modes import runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_current_mode", "file")
    assert is_file_mode() is True
    monkeypatch.setattr(runtime_mod, "_current_mode", "direct")
    assert is_file_mode() is False


def test_prompt_shows_file_tag(monkeypatch):
    from harness.modes import runtime as runtime_mod
    from harness.ui.prompt_input import format_cli_prompt

    monkeypatch.setattr(runtime_mod, "_current_mode", "file")
    assert "|file]" in format_cli_prompt()


def test_tui_file_mode_bypasses_agent_loop(monkeypatch):
    import harness.project.session_registry as registry_mod
    import harness.rag.file_mode as file_mode_mod
    import harness.ui.tui.session as session_mod

    rendered: list[str] = []
    monkeypatch.setattr(file_mode_mod, "is_file_mode", lambda: True)
    monkeypatch.setattr(file_mode_mod, "handle_file_mode_turn", lambda query: "grounded answer")
    monkeypatch.setattr(registry_mod, "touch_session_title_from_query", lambda query: None)
    monkeypatch.setattr(session_mod.renderer, "user", lambda text: None)
    monkeypatch.setattr(session_mod.renderer, "assistant", rendered.append)
    monkeypatch.setattr(
        session_mod,
        "agent_loop",
        lambda *args, **kwargs: pytest.fail("file mode must bypass agent_loop"),
    )

    result = session_mod.run_agent_turn([], {}, "文档里写了什么？")
    assert result["interrupted"] is False
    assert rendered == ["grounded answer"]
