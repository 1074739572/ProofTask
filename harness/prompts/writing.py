"""Writing-mode detection for local RAG thesis/report workflows.

When the user asks to draft or rewrite long Chinese reports using local
reference documents under ``files/``, we append workflow constraints (like
lookup mode) instead of relying on load_skill alone.
"""

from __future__ import annotations

from harness.prompts.lookup import is_lookup_query

_WRITING_HINTS = [
    # Chinese
    "仿写",
    "改写",
    "撰写",
    "结题",
    "技术报告",
    "总结报告",
    "实施方案",
    "研制报告",
    "交付报告",
    "写第",
    "写一章",
    "章节",
    "files/样例",
    "files/",
    "指标与交付",
    "样例",
    "output/",
    # English
    "thesis",
    "rewrite chapter",
    "write chapter",
    "technical report",
    "thesis-writing",
]

_IMPL_HINTS = [
    "改 harness",
    "改 loop",
    "改代码",
    "fix ",
    "implement",
    "refactor",
    "hooks.py",
    "writing_guard",
]


def is_writing_query(query: str) -> bool:
    """True when the user wants local-doc report writing (not web lookup)."""
    if not query or not query.strip():
        return False
    if is_lookup_query(query):
        return False
    low = query.lower()
    if any(k in low for k in _IMPL_HINTS):
        return False
    return any(hint in low for hint in _WRITING_HINTS)


WRITING_CONSTRAINT = (
    "\n\n[Writing mode — auto]\n"
    "语料：本地 ``files/``（样例 docx/md + ``指标与交付要求.md``），用 RAG 检索，"
    "禁止 read_file 整份大 docx。\n"
    "流程：rag_status（已自动索引则跳过）→ 每节写前 **rag_search** → 再 write_file 到 output/。\n"
    "检索：每章 2–3 次针对性 query（结构 / 段落 / 指标）；source/chapter 过滤可选。\n"
    "仿写：学结构和语气，禁止整句照抄；未知数据用 [待补充]。\n"
    "护栏：write_file 到 output/*.md 前必须有本会话 rag_search（WritingGuard）。\n"
)


def augment_query(query: str) -> str:
    if not is_writing_query(query):
        return query
    return query.rstrip() + WRITING_CONSTRAINT
