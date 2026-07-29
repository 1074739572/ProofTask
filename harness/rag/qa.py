"""RAG Q&A: retrieve from selected docs and answer with the active model."""

from __future__ import annotations

import os
from pathlib import Path

from harness.llm import create_message
from harness.rag.pipeline import search
from harness.rag.selection import get_active_sources
from harness.tools.dispatch import extract_text

QA_SYSTEM = """You answer questions using ONLY the provided document excerpts.
Rules:
- If the excerpts do not contain enough information, say what is missing briefly.
- Cite source filenames briefly (basename is enough).
- Do not invent facts not supported by the excerpts.
- Excerpts marked [图片] contain extracted caption/context and, when present, a
  VLM description. Treat an unavailable visual description as unknown rather
  than claiming to have inspected the image.
- Excerpts marked [表格] are table data; preserve numerical values and units.
- Keep the answer concise unless the user asks for detail.
- Respond in the same language as the user's question."""


def _context_char_budget() -> int:
    raw = os.getenv("HARNESS_RAG_QA_CONTEXT_CHARS", "8000").strip()
    try:
        return max(2000, int(raw))
    except ValueError:
        return 8000


def _llm_enabled() -> bool:
    return os.getenv("HARNESS_RAG_QA_LLM", "1").strip().lower() not in ("0", "false", "no")


def _short_source(source: str) -> str:
    return Path(str(source).replace("\\", "/")).name or source


def _format_hit(index: int, hit: dict, *, remaining: int) -> tuple[str, int]:
    source = _short_source(hit.get("source", ""))
    heading = hit.get("heading_path", "")
    # Prefer child snippet only — parent dump makes answers noisy.
    body = (hit.get("text") or "").strip()
    modality = hit.get("modality", "text")
    page = f" › 第{hit['page']}页" if hit.get("page") else ""
    header = f"[{index}] {source} › {heading}{page}" + (
        f" › {modality}" if modality != "text" else ""
    )
    blob = f"{header}\n{body}"
    if len(blob) <= remaining:
        return blob, remaining - len(blob)
    trimmed = blob[: max(0, remaining - 20)] + "\n…[truncated]"
    return trimmed, 0


def build_context(hits: list[dict]) -> str:
    budget = _context_char_budget()
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        if budget <= 0:
            break
        block, budget = _format_hit(index, hit, remaining=budget)
        if block.strip():
            blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def search_for_qa(
    question: str,
    *,
    sources: list[str] | None = None,
    top_k: int = 6,
) -> list[dict]:
    active = sources if sources is not None else get_active_sources()
    if active and len(active) == 1:
        return search(question, top_k=top_k, source=active[0])
    return search(question, top_k=top_k, sources=active)


def _format_citations(hits: list[dict], *, limit: int = 4) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for hit in hits:
        key = f"{_short_source(hit.get('source', ''))}|{hit.get('heading_path', '')}"
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"  · {_short_source(hit.get('source', ''))} › {hit.get('heading_path', '')}"
        )
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def answer_question(
    question: str,
    *,
    sources: list[str] | None = None,
    top_k: int = 6,
) -> str:
    question = question.strip()
    if not question:
        return "Usage: /rag ask <your question>"

    try:
        hits = search_for_qa(question, sources=sources, top_k=top_k)
    except RuntimeError as exc:
        return f"检索失败: {exc}\n请先 /rag index files"

    if not hits:
        scope = get_active_sources()
        if scope:
            return "当前选定文档里没有匹配摘录。可换问题，或说「搜全部」。"
        return "索引里没有匹配摘录。可换关键词，或 /rag docs 查看文档列表。"

    context = build_context(hits)

    # Surface that retrieval actually ran.
    try:
        from harness.ui.renderer import renderer

        preview = ", ".join(
            _short_source(h.get("source", "")) for h in hits[:3] if h.get("source")
        )
        extra = f" · {preview}" if preview else ""
        renderer.muted(f"命中 {len(hits)} 条摘录{extra}")
    except Exception:
        pass

    if not _llm_enabled():
        return f"检索到 {len(hits)} 条：\n\n{context}"

    try:
        response = create_message(
            system=QA_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Document excerpts:\n{context}"
                    ),
                }
            ],
            max_tokens=1200,
        )
        answer = extract_text(response.content).strip()
    except Exception as exc:
        return f"回答失败: {type(exc).__name__}: {exc}\n\n摘录：\n{context}"

    cites = _format_citations(hits)
    if cites:
        return f"{answer}\n\n来源：\n{cites}"
    return answer
