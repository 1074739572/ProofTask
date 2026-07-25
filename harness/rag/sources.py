"""Indexed document listing helpers."""

from __future__ import annotations

from harness.rag.ingest import load_manifest
from harness.rag.selection import load_selection


def list_indexed_sources() -> list[dict]:
    manifest = load_manifest()
    sources = manifest.get("sources") or {}
    selected = set(load_selection())
    rows: list[dict] = []
    for index, (name, meta) in enumerate(sorted(sources.items()), start=1):
        rows.append(
            {
                "index": index,
                "source": name,
                "chunks": meta.get("chunks", 0),
                "parent_chunks": meta.get("parent_chunks", 0),
                "child_chunks": meta.get("child_chunks", 0),
                "chars": meta.get("chars", 0),
                "suffix": meta.get("suffix", ""),
                "selected": name in selected,
            }
        )
    return rows


def resolve_source_numbers(numbers: list[int]) -> list[str]:
    rows = list_indexed_sources()
    by_index = {row["index"]: row["source"] for row in rows}
    resolved: list[str] = []
    for number in numbers:
        source = by_index.get(number)
        if source and source not in resolved:
            resolved.append(source)
    return resolved


def format_docs_list() -> str:
    rows = list_indexed_sources()
    if not rows:
        return (
            "尚无已索引文档。\n"
            "先看本地文件: /rag files\n"
            "再构建索引:   /rag index files"
        )
    from harness.rag.selection import format_selection_summary

    lines = [
        format_selection_summary(),
        "",
        "已索引文档（编号用于 /rag select）:",
    ]
    for row in rows:
        mark = "[x]" if row["selected"] else "[ ]"
        lines.append(
            f"  {row['index']:>2}. {mark} {row['source']} "
            f"({row.get('child_chunks', 0)} child chunks, {row.get('chars', 0)} chars)"
        )
    lines.append("")
    lines.append("选范围: /rag pick  |  /rag select 1,3  |  /rag select all  |  /rag select clear")
    lines.append("看本地上传(含未索引): /rag files")
    return "\n".join(lines)
