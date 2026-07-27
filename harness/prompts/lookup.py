"""Lookup-mode detection and constraint injection.

When the user asks a feasibility / lookup question (e.g. "查某某的 ICML 论文"
or "你可以爬取到吗"), we auto-append a short constraint block to their message
before it reaches the model. This keeps the user's original wording intact while
steering the agent toward "answer first, don't crawl whole sites".

Detection is keyword-based (no model call, no skill load) so it fires every
time without relying on the agent's discretion.

GAIA evals reuse the same LOOKUP_CONSTRAINT (via append_lookup_constraint) so
research discipline is a shared product capability — not an eval-only override.
"""

from __future__ import annotations

# Keywords that signal a lookup / feasibility question (not an implementation task).
# Mixed CN/EN on purpose — users type both.
_LOOKUP_HINTS = [
    # Chinese
    "查找", "查一下", "有没有", "是不是", "是否", "论文", "顶会", "录用",
    "爬取到吗", "能不能爬", "可以爬", "能不能查", "可以查到",
    "搜一下", "搜索", "帮我搜", "有哪些论文", "发过",
    # English
    "find paper", "lookup", "search for", "is there a paper",
    "published at", "accepted at", "icml", "neurips", "iclr", "cvpr",
    "can you crawl", "can you scrape", "can you fetch",
]

# Negations / contexts that mean "actually implement, not lookup".
_IMPLEMENTMENT_HINTS = [
    "改", "修", "实现", "重构", "添加", "删除", "写一个", "写代码",
    "加一个", "加个", "fix", "implement", "refactor", "add ", "edit ",
    "write_file", "edit_file", "loop.py", "hooks.py",
]


def is_lookup_query(query: str) -> bool:
    """True when a user message looks like a lookup/feasibility question.

    Heuristic: contains a lookup keyword AND is not dominated by implementation
    keywords. Short queries with a lookup verb win; long code-change requests
    that happen to mention "论文" lose.
    """
    if not query or not query.strip():
        return False
    low = query.lower()
    has_lookup = any(k in low for k in _LOOKUP_HINTS)
    if not has_lookup:
        return False
    # If the message is clearly about modifying code, don't hijack it.
    impl_hits = sum(1 for k in _IMPLEMENTMENT_HINTS if k in low)
    lookup_hits = sum(1 for k in _LOOKUP_HINTS if k in low)
    # Implementation intent outweighs a stray lookup keyword.
    if impl_hits >= 2 and impl_hits >= lookup_hits:
        return False
    return True


# Shared research discipline: used by daily CLI lookup AND GAIA eval.
# Keep format-agnostic (no FINAL ANSWER) — scoring templates stay in evals/.

# Mode A: unit/format misreads (e.g. "how many thousand hours" → 17 not 17000).
ANSWER_BOUNDARY_CHECK = (
    "Before committing an answer, re-check:\n"
    "1. UNIT — does the question ask for thousands / millions / a rounded "
    "value / a count of items? Answer in THAT unit (e.g. \"how many "
    "thousand hours\" → thousands, not raw hours).\n"
    "2. FORMAT — no thousands-separators in numbers unless asked; no $/% "
    "unless asked; strings without articles/abbreviations when a short "
    "canonical form is required.\n"
    "3. TYPE — title vs name vs number vs comma-list: match what was asked.\n"
)

RESEARCH_DISCIPLINE = (
    "读题（先复述再动手）：用一两句话写清——问的是什么（数量/名称/标题/列表）、"
    "单位与格式边界（如「多少千小时」答千为单位 ≠ 小时总数；四舍五入、"
    "不要逗号、First M. Last、as it appears 等）。复述错则整题错。\n"
    f"{ANSWER_BOUNDARY_CHECK}"
    "流程：web_search 发现 URL → fetch 1–2 个有希望的页 → "
    "真正读完页面再决定要不要再搜。未读就再搜 = 浪费预算。\n"
    "证据：具体事实（精确标题、数字、场次名、作者列表）只信任"
    "本轮已 fetch 并读到的内容；不要把训练记忆当成已验证事实。"
    "查不到就说查不到或给出可辩护的猜测，并标明未验证。\n"
    "换源：单 URL 404/robots/403 时换站点或策略"
    "（论文→arxiv/scholar；维基→API/摘要页），禁止近重复换皮查询、"
    "禁止反复锤同一 host。\n"
)

LOOKUP_CONSTRAINT = (
    "\n\n[Lookup mode — auto]\n"
    "成功标准：找到就给标题+来源链接；找不到就明确说「公开检索未找到」，"
    "并简要列出易混名/易混单位。\n"
    f"{RESEARCH_DISCIPLINE}"
    "槽位：作者/会议/年份等关键检索条件若仍模糊且上下文消不了，"
    "先用纯文字问用户 1–3 个问题，禁止用 fetch/bash 参数去猜。\n"
    "手段：优先调用内置 web_search（Bing/360）；有线索后再对具体 URL 用 "
    "mcp__fetch__fetch。禁止对 google.com / baidu.com / bing.com 搜索页做 fetch"
    "（robots/验证码会失败）。OpenReview / OpenAlex / 学院官网直接开页即可。\n"
    "禁止反复抓同一大页（如会议 Schedule / proceedings 目录）塞进对话。\n"
    "效率：有答案就收口，不要为凑证据无限联网；连续无新信息就换策略或回答。"
    "没有固定次数硬顶（除非环境变量开了 fetch limit）。\n"
    "收口：先回答用户「有/没有」，再附来源；不要为了「拿证据」而无限抓页。\n"
    "篇幅：最终回答 ≤8 行；追问单位/作者时用 1–3 句直接说清归属，"
    "禁止再铺大表或复述整篇检索过程。\n"
    "护栏：LookupGuard 会拦截近重复搜索、连续无效搜索、已失败 URL/host；"
    "被拦后必须文字回答用户（用本轮已读到的证据，不要编造具体细节）。\n"
)

_LOOKUP_MARKER = "[Lookup mode — auto]"


def append_lookup_constraint(text: str) -> str:
    """Append LOOKUP_CONSTRAINT if not already present (idempotent)."""
    if not text:
        return LOOKUP_CONSTRAINT.strip()
    if _LOOKUP_MARKER in text:
        return text
    return text.rstrip() + LOOKUP_CONSTRAINT


def is_lookup_active(query: str | None = None) -> bool:
    """True when the current context is lookup (keyword or explicit /mode lookup)."""
    from harness.modes import get_mode

    if get_mode() == "lookup":
        return True
    if query and is_lookup_query(query):
        return True
    return False


def augment_query(query: str) -> str:
    """Return the user query with a lookup constraint appended when appropriate.

    The original wording is always preserved verbatim at the front; the
    constraint is appended so the model sees both the intent and the rules.
    """
    if not is_lookup_active(query):
        return query
    return append_lookup_constraint(query)
