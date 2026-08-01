"""File tools: read_file / write_file / edit_file / glob behavior."""

import pytest

from harness.tools.filesystem import (
    MAX_READ_CHARS,
    MAX_WRITE_CHARS,
    run_edit,
    run_glob,
    run_read,
    run_write,
    safe_path,
)


# ---------- read_file ----------

def test_read_full_header_and_line_numbers(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = run_read("a.txt", cwd=tmp_path)
    assert out.startswith("3 lines total")
    assert "     1 | one" in out
    assert "     3 | three" in out


def test_read_windowed_with_offset_and_footer(tmp_path):
    (tmp_path / "a.txt").write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    out = run_read("a.txt", limit=2, offset=1, cwd=tmp_path)
    assert out.startswith("5 lines total, showing 2-3")
    assert "     2 | l2" in out
    assert "     3 | l3" in out
    assert "... (2 more lines)" in out
    assert "l1" not in out.splitlines()[1]  # first numbered line is #2


def test_read_offset_past_end(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    out = run_read("a.txt", offset=10, cwd=tmp_path)
    assert "past the end" in out


def test_read_directory_rejected(tmp_path):
    (tmp_path / "sub").mkdir()
    out = run_read("sub", cwd=tmp_path)
    assert "directory" in out


def test_read_binary_rejected(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
    out = run_read("b.bin", cwd=tmp_path)
    assert "binary" in out


def test_read_gbk_fallback(tmp_path):
    (tmp_path / "g.txt").write_bytes("你好\n世界\n".encode("gbk"))
    out = run_read("g.txt", cwd=tmp_path)
    assert "2 lines total" in out
    assert "你好" in out


def test_read_empty_file(tmp_path):
    (tmp_path / "e.txt").write_text("", encoding="utf-8")
    out = run_read("e.txt", cwd=tmp_path)
    assert out == "0 lines total"


def test_read_escape_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_read("../outside.txt", cwd=tmp_path)


def test_read_full_file_truncation_cap(tmp_path):
    (tmp_path / "big.txt").write_text("a" * (MAX_READ_CHARS + 100), encoding="utf-8")
    out = run_read("big.txt", cwd=tmp_path)
    assert "truncated after" in out
    assert len(out) < MAX_READ_CHARS + 500


# ---------- write_file ----------

def test_write_creates_dirs_and_reports_bytes(tmp_path):
    out = run_write("d/e/f.txt", "hello", cwd=tmp_path)
    assert out == "Wrote 5 bytes to d/e/f.txt"
    assert (tmp_path / "d" / "e" / "f.txt").read_text(encoding="utf-8") == "hello"


def test_write_size_cap(tmp_path):
    out = run_write("big.txt", "x" * (MAX_WRITE_CHARS + 1), cwd=tmp_path)
    assert "too large" in out
    assert not (tmp_path / "big.txt").exists()


# ---------- edit_file ----------

def test_edit_single_occurrence(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    out = run_edit("e.txt", "b", "B", cwd=tmp_path)
    assert out == "Edited e.txt"
    assert p.read_text(encoding="utf-8") == "a\nB\nc\n"


def test_edit_nth_occurrence(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("x\ny\nx\n", encoding="utf-8")
    out = run_edit("e.txt", "x", "X", occurrence=2, cwd=tmp_path)
    assert "replaced occurrence 2 of 2" in out
    assert p.read_text(encoding="utf-8") == "x\ny\nX\n"


def test_edit_occurrence_out_of_range(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("x\n", encoding="utf-8")
    out = run_edit("e.txt", "x", "X", occurrence=3, cwd=tmp_path)
    assert "out of range" in out and "1 time(s)" in out
    assert p.read_text(encoding="utf-8") == "x\n"


def test_edit_mismatch_hint(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("alpha\nbeta\n", encoding="utf-8")
    out = run_edit("e.txt", "betta", "z", cwd=tmp_path)
    assert "text not found" in out
    assert "Closest match: line 2: beta" in out
    assert p.read_text(encoding="utf-8") == "alpha\nbeta\n"


# ---------- glob ----------

def test_glob_recursive(tmp_path):
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.py").write_text("", encoding="utf-8")
    out = run_glob("**/*.py", cwd=tmp_path)
    assert "top.py" in out
    assert "nested/deep.py" in out or "nested\\deep.py" in out


def test_glob_no_recursive_match_without_starstar(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.py").write_text("", encoding="utf-8")
    out = run_glob("*.py", cwd=tmp_path)
    assert out == "(no matches)"


# ---------- safe_path ----------

def test_safe_path_allows_inside_blocks_outside(tmp_path):
    inside = safe_path("sub/x.txt", cwd=tmp_path)
    assert str(inside).endswith("sub/x.txt")
    with pytest.raises(ValueError):
        safe_path(str(tmp_path.parent / "x.txt"), cwd=tmp_path)
