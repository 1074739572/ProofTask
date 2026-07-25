"""CLI commands for manual RAG corpus management."""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

from harness.rag.bootstrap import ensure_rag_indexed
from harness.rag.config import SUPPORTED_SUFFIXES
from harness.rag.corpus import format_files_list, format_rag_overview
from harness.rag.doc_picker import run_doc_picker
from harness.rag.ingest import resolve_path
from harness.rag.qa import answer_question
from harness.rag.selection import (
    SCOPE_ALL,
    clear_selection,
    format_selection_summary,
    load_selection,
    set_scope,
    set_selection,
)
from harness.rag.sources import format_docs_list, resolve_source_numbers
from harness.rag.tools import run_rag_index, run_rag_status

# Canonical subcommand → aliases (including common typos).
_SUBCOMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "help": ("help", "?", "h"),
    "files": ("files", "file", "ls", "list", "corpus", "uploads", "local"),
    "docs": ("docs", "doc", "documents", "sources"),
    "status": ("status", "stat", "info", "stats"),
    "pick": ("pick", "picker", "multi", "choose"),
    "select": ("select", "sel", "selection", "selcect", "selec", "seelct", "slect"),
    "ask": ("ask", "query", "qa", "question"),
    "index": ("index", "build", "refresh", "reindex"),
    "add": ("add", "import", "upload"),
    "reset": ("reset", "wipe", "clear-index"),
}

_CANONICAL_BY_ALIAS: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _SUBCOMMAND_ALIASES.items()
    for alias in aliases
}


def _cwd() -> Path:
    return Path.cwd()


def _corpus_dir() -> Path:
    return _cwd() / "files"


def _rag_dir() -> Path:
    return _cwd() / ".rag"


def _resolve_subcommand(raw: str) -> tuple[str | None, str | None]:
    """Map user token → canonical subcommand.

    Returns (canonical, suggestion). suggestion is set when no exact/alias hit
    but a close match exists.
    """
    token = (raw or "").strip().lower()
    if not token:
        return None, None
    if token in _CANONICAL_BY_ALIAS:
        return _CANONICAL_BY_ALIAS[token], None
    # Close matches against all known aliases + canonical names.
    pool = sorted(set(_CANONICAL_BY_ALIAS) | set(_SUBCOMMAND_ALIASES))
    matches = difflib.get_close_matches(token, pool, n=1, cutoff=0.6)
    if matches:
        hit = matches[0]
        canonical = _CANONICAL_BY_ALIAS.get(hit, hit if hit in _SUBCOMMAND_ALIASES else None)
        if canonical:
            return None, canonical
    return None, None


def rag_help_text() -> str:
    corpus = _corpus_dir()
    return f"""RAG / file mode:

  /mode file                enter file mode (every turn: retrieve then answer)
  /mode direct              back to direct Agent mode

  /rag                      local files + index status overview
  /rag help                 this command list
  /rag files                list local uploads under files/ (indexed or not)
  /rag docs                 list indexed documents (numbered for select)
  /rag status               index stats
  /rag pick                 multi-select docs for file-mode scope
  /rag select 1,3|all|clear set scope by number / all / clear(=all)
  /rag ask <question>       one-shot Q&A (in file mode just type the question)
  /rag index [path]         build or refresh index (default: files/)
  /rag add <file|dir>       import file or directory of docs into files/ then index
  /rag reset                wipe .rag/ (clean rebuild)

Corpus: {corpus}
Index:   {_rag_dir()}

Recommended: /rag files → /rag index files → /mode file → ask → /mode direct"""


def _normalize_user_path(raw: str) -> Path:
    text = raw.strip().strip('"').strip("'")
    path = Path(text)
    if path.is_absolute():
        return path
    return (_cwd() / path).resolve()


def run_rag_add(source: str) -> str:
    """Import a document or directory into files/ and re-index."""
    if not source.strip():
        return "Usage: /rag add <path-to-file-or-dir>"

    src = _normalize_user_path(source)
    if not src.exists():
        return f"rag add failed: path not found: {src}"

    dest_dir = _corpus_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    if src.is_dir():
        files = [
            path
            for path in sorted(src.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        if not files:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            return (
                f"rag add failed: no supported files under {src}\n"
                f"(supported: {supported})"
            )
        # Keep a named subfolder so many manuals don't flatten into files/.
        folder_name = src.name.strip() or "imported"
        target_root = dest_dir / folder_name
        target_root.mkdir(parents=True, exist_ok=True)
        copied = 0
        for path in files:
            rel = path.relative_to(src)
            dest = target_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size == path.stat().st_size:
                continue
            shutil.copy2(path, dest)
            copied += 1
        lines.append(
            f"已从目录导入 {copied} 个新文件（共发现 {len(files)} 个可索引）→ {target_root}"
        )
    elif src.is_file():
        suffix = src.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            return f"rag add failed: unsupported suffix {suffix!r} (supported: {supported})"

        try:
            src.relative_to(dest_dir.resolve())
            already_inside = True
        except ValueError:
            already_inside = False

        if already_inside:
            lines.append(f"已在 files/ 内，不再复制: {src}")
            lines.append(
                "提示：对 files/ 子目录里的文件请用 /rag index files，"
                "勿 /rag add（会在根目录再拷一份）。"
            )
        else:
            dest = dest_dir / src.name
            if dest.exists() and dest.resolve() != src.resolve():
                lines.append(f"覆盖已有: {dest.name}")
            shutil.copy2(src, dest)
            lines.append(f"已导入: {dest}")
    else:
        return f"rag add failed: not a file or directory: {src}"

    lines.append("")
    result = ensure_rag_indexed("files")
    lines.append(result.get("message") or str(result))
    if not result.get("ok"):
        return "\n".join(lines)
    lines.append("")
    lines.append(format_files_list("files"))
    return "\n".join(lines)


def run_rag_reset() -> str:
    """Delete .rag/ artifacts so the next index starts clean."""
    import shutil as _shutil

    from harness.rag.config import RAG_DIR
    from harness.rag.pipeline import reset_runtime
    from harness.rag.selection import clear_selection

    reset_runtime()
    if RAG_DIR.exists():
        _shutil.rmtree(RAG_DIR, ignore_errors=True)
    clear_selection()
    return (
        f"已清空索引目录: {RAG_DIR}\n"
        "本地 files/ 里的上传文件还在。下一步：/rag files 查看 → /rag index files"
    )


def run_rag_index_command(path: str = "files") -> str:
    """Index a corpus path without going through the agent tool."""
    target = (path or "files").strip()
    if not target:
        target = "files"
    try:
        resolved = resolve_path(target)
    except Exception as exc:
        return f"rag index failed: {type(exc).__name__}: {exc}"

    if not resolved.exists():
        return (
            f"rag index failed: path not found: {resolved}\n"
            f"Create {_corpus_dir()} and add .md/.txt/.docx/.pdf, "
            "or use /rag add <file>."
        )

    result = ensure_rag_indexed(target)
    if result.get("ok"):
        body = result.get("message") or run_rag_index(target)
        return f"{body}\n\n{format_files_list(target if target else 'files')}"
    return result.get("message") or f"rag index failed: {result}"


def run_rag_select_command(spec: str = "") -> str:
    text = spec.strip()
    if not text:
        parts = [format_selection_summary(), "", format_docs_list()]
        return "\n".join(parts)

    lowered = text.lower()
    if lowered in ("clear", "none", "reset"):
        clear_selection()
        return "已设为搜全部已索引文档（selection cleared）。"
    if lowered == "all":
        set_scope(SCOPE_ALL)
        return "已设为搜全部已索引文档。"

    numbers: list[int] = []
    for token in re.split(r"[,;\s]+", text):
        token = token.strip()
        if token.isdigit():
            numbers.append(int(token))

    if not numbers:
        return (
            f"Unknown selection spec: {spec!r}\n"
            "Use: /rag select 1,3  |  /rag select all  |  /rag select clear"
        )

    resolved = resolve_source_numbers(numbers)
    if not resolved:
        return (
            f"No documents matched: {spec}\n"
            "Run /rag docs to see valid numbers (must index first: /rag index files)."
        )

    chosen = set_selection(resolved)
    lines = [f"已指定 {len(chosen)} 个文档（文件模式只在这些里检索）:"]
    lines.extend(f"  - {name}" for name in chosen)
    return "\n".join(lines)


def run_rag_ask_command(question: str) -> str:
    if not question.strip():
        return (
            f"Usage: /rag ask <your question>\n\n{format_selection_summary()}\n\n"
            "Tip: /mode file 后可直接提问；或 /rag pick 指定文档。"
        )
    return answer_question(question)


def run_rag_cli_command(query: str) -> str:
    """Handle /rag and subcommands from the interactive CLI."""
    parts = query.strip().split()
    if len(parts) == 1:
        return format_rag_overview()

    raw_sub = parts[1]
    sub, suggestion = _resolve_subcommand(raw_sub)
    if sub is None and suggestion:
        # Auto-correct close typos (e.g. selcect → select) and continue.
        sub = suggestion
        note = f"已将 `{raw_sub}` 理解为 `/rag {sub}`。\n\n"
    else:
        note = ""

    if sub is None:
        hint = ""
        if suggestion:
            hint = f"\nDid you mean: /rag {suggestion} ?"
        return (
            f"Unknown /rag subcommand: {raw_sub}{hint}\n\n"
            f"{rag_help_text()}"
        )

    if sub == "help":
        return rag_help_text()
    if sub == "files":
        return format_files_list()
    if sub == "status":
        return run_rag_status()
    if sub == "docs":
        return format_docs_list()
    if sub == "pick":
        return note + run_doc_picker()
    if sub == "select":
        spec = query.strip().split(maxsplit=2)[2] if len(parts) > 2 else ""
        return note + run_rag_select_command(spec)
    if sub == "ask":
        question = query.strip().split(maxsplit=2)[2] if len(parts) > 2 else ""
        return note + run_rag_ask_command(question)
    if sub == "index":
        path = parts[2] if len(parts) > 2 else "files"
        return note + run_rag_index_command(path)
    if sub == "reset":
        return note + run_rag_reset()
    if sub == "add":
        if len(parts) < 3:
            return "Usage: /rag add <path-to-file>"
        path = query.strip().split(maxsplit=2)[2]
        return note + run_rag_add(path)

    return (
        f"Unknown /rag subcommand: {raw_sub}\n\n"
        f"{rag_help_text()}"
    )
