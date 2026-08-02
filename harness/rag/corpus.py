"""Local corpus inventory: on-disk files vs indexed status."""

from __future__ import annotations

from pathlib import Path

from harness.rag.bootstrap import DEFAULT_INDEX_PATH, index_refresh_reason
from harness.rag.ingest import discover_files, load_manifest, resolve_path
from harness.rag.selection import format_selection_summary, load_selection


def list_corpus_inventory(path: str = DEFAULT_INDEX_PATH) -> list[dict]:
    """Return rows for every on-disk corpus file plus orphaned index entries.

    Each row: index, source, path, state ('indexed'|'stale'|'pending'|'missing'),
    chunks, chars, selected.
    """
    from harness.rag.ingest import WORKDIR as _ingest_workdir

    try:
        root = resolve_path(path)
    except Exception:
        root = _ingest_workdir / "files"

    manifest = load_manifest()
    sources = dict(manifest.get("sources") or {})
    selected = set(load_selection())

    by_path: dict[str, tuple[str, dict]] = {}
    for name, meta in sources.items():
        raw = meta.get("path")
        if not raw:
            continue
        try:
            by_path[str(Path(raw).resolve())] = (name, meta)
        except OSError:
            continue

    rows: list[dict] = []
    seen_names: set[str] = set()
    files = discover_files(root) if root.exists() else []

    for file in files:
        try:
            key = str(file.resolve())
            rel = str(file.relative_to(root)) if root.is_dir() else file.name
        except (OSError, ValueError):
            key = str(file)
            rel = file.name

        if key in by_path:
            name, meta = by_path[key]
            seen_names.add(name)
            try:
                mtime = file.stat().st_mtime
            except OSError:
                mtime = 0.0
            previous = float(meta.get("mtime", 0) or 0)
            state = "indexed" if abs(mtime - previous) <= 1e-6 else "stale"
            rows.append(
                {
                    "source": name,
                    "path": key,
                    "rel": rel,
                    "state": state,
                    "chunks": int(meta.get("child_chunks") or meta.get("chunks") or 0),
                    "chars": int(meta.get("chars") or 0),
                    "selected": name in selected,
                    "suffix": meta.get("suffix") or file.suffix.lower(),
                }
            )
        else:
            rows.append(
                {
                    "source": rel.replace("\\", "/"),
                    "path": key,
                    "rel": rel,
                    "state": "pending",
                    "chunks": 0,
                    "chars": 0,
                    "selected": False,
                    "suffix": file.suffix.lower(),
                }
            )

    for name, meta in sorted(sources.items()):
        if name in seen_names:
            continue
        rows.append(
            {
                "source": name,
                "path": str(meta.get("path") or ""),
                "rel": name,
                "state": "missing",
                "chunks": int(meta.get("child_chunks") or meta.get("chunks") or 0),
                "chars": int(meta.get("chars") or 0),
                "selected": name in selected,
                "suffix": meta.get("suffix") or "",
            }
        )

    for index, row in enumerate(rows, start=1):
        row["index"] = index
    return rows


def _skipped_unsupported_summary(root: Path) -> str:
    """Summarize on-disk files that RAG will not index (e.g. .png / .py)."""
    from collections import Counter

    from harness.rag.config import SUPPORTED_SUFFIXES

    if not root.exists() or not root.is_dir():
        return ""
    counts: Counter[str] = Counter()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower() or "(no-ext)"
        if suffix not in SUPPORTED_SUFFIXES:
            counts[suffix] += 1
    if not counts:
        return ""
    parts = [f"{ext}×{n}" for ext, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return (
        "同目录还有未纳入语料的文件（不会出现在上表）: "
        + ", ".join(parts)
        + "\n可索引后缀仅: "
        + ", ".join(sorted(SUPPORTED_SUFFIXES))
    )


def format_files_list(path: str = DEFAULT_INDEX_PATH) -> str:
    """Human-readable local corpus inventory with index marks."""
    from harness.rag.ingest import WORKDIR as _ingest_workdir

    try:
        root = resolve_path(path)
    except Exception:
        root = _ingest_workdir / "files"

    rows = list_corpus_inventory(path)
    reason = index_refresh_reason(path if root.exists() else None)

    lines: list[str] = [
        f"本地语料目录: {root}",
        format_selection_summary(),
    ]
    if reason:
        lines.append(f"索引状态: 需刷新 — {reason}")
        if "语料目录已切换" in reason or "索引为空" in (reason or ""):
            lines.append(
                "提示: 当前 .rag 索引不是这份 files/ 的快照。"
                "建议 /rag reset 后执行 /rag index files。"
            )
    elif rows and any(r["state"] == "indexed" for r in rows):
        lines.append("索引状态: 最新")
    else:
        lines.append("索引状态: 空（尚未 /rag index）")

    lines.append("")
    if not rows:
        lines.append("（目录为空）把 .md/.txt/.docx/.pdf 放进 files/，或用 /rag add <文件>")
        lines.append("然后执行: /rag index files")
        skipped = _skipped_unsupported_summary(root)
        if skipped:
            lines.append(skipped)
        return "\n".join(lines)

    state_mark = {
        "indexed": "OK已索引",
        "stale": "需刷新",
        "pending": "未索引",
        "missing": "文件丢失",
    }
    lines.append("编号  状态      文件")
    for row in rows:
        mark = state_mark.get(row["state"], row["state"])
        sel = "*" if row["selected"] else " "
        extra = ""
        if row["state"] in ("indexed", "stale", "missing") and row.get("chunks"):
            extra = f"  ({row['chunks']} chunks)"
        lines.append(
            f"  {row['index']:>2}.{sel} [{mark}] {row['source']}{extra}"
        )

    pending = sum(1 for r in rows if r["state"] in ("pending", "stale", "missing"))
    indexed = sum(1 for r in rows if r["state"] == "indexed")
    pdf_n = sum(1 for r in rows if str(r.get("suffix") or "").lower() == ".pdf"
                or str(r.get("source") or "").lower().endswith(".pdf"))
    lines.append("")
    lines.append(f"合计: {len(rows)} 个可索引文件 · 已索引 {indexed} · 待处理 {pending}")
    if pdf_n == 0:
        lines.append(
            "说明: files/ 里当前没有 .pdf。"
            "若 PDF 在别的目录，用 /rag add D:\\path\\to\\file.pdf 导入后再 index。"
        )
    skipped = _skipped_unsupported_summary(root)
    if skipped:
        lines.append(skipped)
    lines.append("下一步:")
    if pending or reason:
        lines.append("  /rag reset                清掉旧索引（可选，目录切换时建议）")
        lines.append("  /rag index files          构建/刷新索引")
    lines.append("  /rag docs                 只看已索引文档（选范围用编号）")
    lines.append("  /rag select 1,3|all|clear 设检索范围")
    lines.append("  /rag pick                 交互多选")
    lines.append("  /mode file                进入文件问答模式")
    return "\n".join(lines)


def format_rag_overview(path: str = DEFAULT_INDEX_PATH) -> str:
    """Compact dashboard for bare `/rag` — what is on disk + what to do next."""
    inventory = format_files_list(path)
    tips = (
        "\n常用命令:\n"
        "  /rag files                 本地上传/语料清单（上表）\n"
        "  /rag docs                  已索引文档编号列表\n"
        "  /rag select 1,3|all|clear  设定检索范围\n"
        "  /rag pick                  交互多选文档\n"
        "  /rag index files           构建或刷新索引\n"
        "  /rag add <path>            导入外部文件或目录到 files/ 并索引\n"
        "  /rag status                索引统计\n"
        "  /rag help                  完整帮助\n"
        "  /mode file                 进入文件模式直接提问"
    )
    return f"{inventory}\n{tips}"
