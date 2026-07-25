"""Tests for manual /rag CLI commands."""

from pathlib import Path

import pytest

from harness.rag.commands import (
    _resolve_subcommand,
    run_rag_add,
    run_rag_cli_command,
    run_rag_index_command,
)
from tests.rag_fixtures import rag_env  # noqa: F401

FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "rag" / "fixtures" / "tiny_corpus"
SAMPLE = FIXTURE / "sample_report.md"


@pytest.fixture()
def isolated_cwd(rag_env, tmp_path, monkeypatch):
    import harness.rag.ingest as ingest_mod
    import harness.settings as settings_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_mod, "WORKDIR", tmp_path)
    monkeypatch.setattr(ingest_mod, "WORKDIR", tmp_path)
    return tmp_path


def test_rag_help():
    text = run_rag_cli_command("/rag help")
    assert "/rag index" in text
    assert "/rag add" in text
    assert "/rag files" in text


def test_rag_overview_lists_local_files(isolated_cwd):
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "upload_me.md").write_text("# hello\n\nbody", encoding="utf-8")
    text = run_rag_cli_command("/rag")
    assert "upload_me.md" in text
    assert "未索引" in text


def test_rag_files_and_typo_select(isolated_cwd):
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha doc", encoding="utf-8")
    (corpus / "b.md").write_text("beta doc", encoding="utf-8")
    run_rag_index_command("files")

    files_text = run_rag_cli_command("/rag files")
    assert "a.md" in files_text
    assert "b.md" in files_text
    assert "已索引" in files_text

    # Typo alias + numbered select
    assert _resolve_subcommand("selcect") == ("select", None)
    selected = run_rag_cli_command("/rag selcect 1")
    assert "已指定" in selected
    assert "a.md" in selected


def test_rag_unknown_subcommand():
    text = run_rag_cli_command("/rag no_such_sub")
    assert "Unknown /rag subcommand" in text


def test_rag_index_fixture(isolated_cwd):
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "sample_report.md").write_text(
        SAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    text = run_rag_index_command("files")
    assert "Indexed corpus" in text
    assert "sample_report.md" in text


def test_rag_status_after_index(isolated_cwd):
    corpus = isolated_cwd / "files"
    corpus.mkdir()
    (corpus / "metrics.md").write_text("## 指标\n\nFY-4 全通道。", encoding="utf-8")
    run_rag_index_command("files")
    text = run_rag_cli_command("/rag status")
    assert "metrics.md" in text


def test_rag_add_copies_and_indexes(isolated_cwd):
    external = isolated_cwd / "external.md"
    external.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    text = run_rag_add(str(external))
    assert "已导入:" in text
    assert (isolated_cwd / "files" / "external.md").exists()
    assert "Indexed corpus" in text
