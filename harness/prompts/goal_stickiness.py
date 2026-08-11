"""Keep the model on the user's stated task across follow-ups and corrections."""

from __future__ import annotations

import re

_CORRECTION_RE = re.compile(
    r"(?:"
    r"我让你|你在干什么|不是让你|我说的是|我说了|走偏了|跑偏|"
    r"别再|不要再|停下.*改|继续运行|继续做|继续配|"
    r"you(?:'re| are) (?:doing|off)|wrong (?:task|thing)|"
    r"i (?:told|asked) you to|stay on|don'?t (?:switch|change)"
    r")",
    re.IGNORECASE,
)

# Short replies that are usually slot-fills for a pending question.
_YES_NO_RE = re.compile(
    r"^(?:yes|no|y|n|ok|好|行|可以|确认|用这个)\s*$",
    re.IGNORECASE,
)
_MODELISH_RE = re.compile(
    r"^(?:deepseek|gpt|qwen|claude|llama|vanna|openai|gemini)[\w\-\.]*$",
    re.IGNORECASE,
)
_PATHISH_RE = re.compile(r"^(?:[a-zA-Z]:\\|/).{2,200}$")

_STICKINESS_TAIL = (
    "\n\n[Harness] Stay on the user's Working goal.\n"
    "- If they are correcting you (「我让你…」「你在干什么」), hard-reset to "
    "THAT task; drop the detour.\n"
    "- If they answered a choice you offered (model / data / path / yes-no), "
    "apply the answer and continue the SAME pending task — do not reinterpret "
    "it as a new exploration of this harness.\n"
    "- Do not treat this repo's `main.py` / `harness/cli.py` as the user's "
    "product (e.g. Vanna, DB-GPT, deepseek_mysql.py) unless they explicitly "
    "asked to change the harness itself.\n"
    "- Prefer their named script/package; ask one clarifying question only if "
    "the entrypoint is still unknown after a quick search."
)


def looks_like_correction(query: str) -> bool:
    return bool(_CORRECTION_RE.search(query or ""))


def looks_like_slot_fill(query: str, *, has_pending_question: bool = False) -> bool:
    """Recognize a short answer only when the harness has a pending question."""
    if not has_pending_question:
        return False
    text = (query or "").strip()
    if not text or len(text) > 60:
        return False
    if "。" in text or "?" in text or "？" in text or "！" in text:
        return False
    if _YES_NO_RE.match(text) or _MODELISH_RE.match(text) or _PATHISH_RE.match(text):
        return True
    # Bare token (model id, db name) — no spaces, short.
    if len(text) <= 24 and not any(c.isspace() for c in text):
        return True
    return False


def augment_if_needed(query: str, *, has_pending_question: bool = False) -> str | None:
    """Return query + stickiness constraint, or None when not needed."""
    text = (query or "").strip()
    if not text:
        return None
    if looks_like_correction(text) or looks_like_slot_fill(
        text, has_pending_question=has_pending_question
    ):
        return text + _STICKINESS_TAIL
    return None
