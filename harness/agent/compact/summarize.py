"""LLM summarization for compacted conversation history."""

from __future__ import annotations

import json

from harness.agent.compact.sizing import _safe_input_budget
from harness.llm import create_message
from harness.messages.blocks import block_field, block_type
from harness.models import get_model
from harness.settings import WORKDIR
from harness.tools.dispatch import extract_text

# Returned when the summarizer produces no usable text. Pipeline must NOT
# replace history with "[Compacted]\\n\\n(empty summary)" — that causes amnesia.
SUMMARY_UNUSABLE = "__COMPACT_SUMMARY_UNUSABLE__"

_STRUCTURED_SUMMARY_SECTIONS = (
    "## Goal",
    "## User Constraints",
    "## Changed Files",
    "## Key Findings",
    "## Sources Tried",
    "## Facts Gathered",
    "## Remaining Work",
    "## Do NOT Forget",
)

# Reasoning models (e.g. deepseek-v4-flash) often put the whole answer in a
# thinking/reasoning block and leave type=text empty. extract_text only reads
# text blocks → "(empty summary)" → agent amnesia after compact (GAIA Pie Menus).
_THINKING_TYPES = frozenset({"thinking", "reasoning", "redacted_thinking"})


def _structured_summary_instruction() -> str:
    sections = "\n".join(_STRUCTURED_SUMMARY_SECTIONS)
    return (
        f"Working directory: {WORKDIR}\n"
        "Python package lives at `harness/` in this repo (NOT `src/harness/`).\n"
        "Summarize this agent conversation so work can continue after compaction.\n"
        "Use exactly these markdown headings and fill each section:\n"
        f"{sections}\n\n"
        "Guidelines:\n"
        "- Goal: MUST reflect the chronologically latest real user instruction, "
        "not an older task from earlier in the conversation.\n"
        "- If the latest user message conflicts with earlier goals or a previous "
        "summary, the latest user message wins — say so in Goal and Do NOT Forget.\n"
        "- User Constraints: explicit requirements, paths, formats, tone, deadlines.\n"
        "- Changed Files: bullet list of paths touched and what changed.\n"
        "- Key Findings: facts, errors, metrics, or decisions that must survive compaction.\n"
        "- Sources Tried: URLs / queries attempted and outcome "
        "(ok / 404 / robots / blocked / irrelevant). Critical for lookup tasks.\n"
        "- Facts Gathered: concrete facts already obtained WITH their source "
        "(so the agent does not re-search the same thing after compact).\n"
        "- Remaining Work: concrete next steps; include drift complaints or wrong paths.\n"
        "- For lookup / paper-search tasks: if sources failed or budget is spent, "
        "Remaining Work MUST be 'answer 有/没有 now' — never 'try more URLs'.\n"
        "- Do NOT Forget: non-negotiables the agent must not drop (e.g. sample doc paths).\n"
        "- Put the full structured markdown in the normal assistant text reply "
        "(not only in hidden reasoning). Empty text is a failure.\n"
    )


def extract_summary_text(content) -> str:
    """Pull summarizer output from text blocks, falling back to thinking blocks.

    Reasoning models frequently write the summary only into ``thinking`` /
    ``reasoning`` content blocks. Using ``extract_text`` alone yields "" and
    the pipeline used to store ``(empty summary)`` — wiping all prior facts.
    """
    text = extract_text(content).strip()
    if text:
        return text
    if not isinstance(content, list):
        return str(content or "").strip()
    parts: list[str] = []
    for block in content:
        btype = block_type(block)
        if btype not in _THINKING_TYPES:
            continue
        chunk = (
            block_field(block, "thinking", None)
            or block_field(block, "text", None)
            or block_field(block, "reasoning", None)
            or ""
        )
        chunk = str(chunk).strip()
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()


def is_summary_unusable(summary: str) -> bool:
    """True when compact must degrade instead of injecting this as the history."""
    if not summary or not str(summary).strip():
        return True
    s = str(summary).strip()
    if s == SUMMARY_UNUSABLE:
        return True
    if s == "(empty summary)":
        return True
    if s.startswith("[Compact summary unavailable:"):
        return True
    return False


def summarize_history(messages: list) -> str:
    budget = _safe_input_budget()
    # Compaction happens because the current work is too long. The old head is
    # already represented by earlier summaries, so preserve whole recent
    # messages instead of slicing the beginning of a JSON document.
    selected: list = []
    used = 2  # []
    for message in reversed(messages):
        encoded = json.dumps(message, default=str, ensure_ascii=False)
        addition = len(encoded) + (1 if selected else 0)
        if selected and used + addition > budget:
            break
        if not selected and addition > budget:
            selected.append({"role": message.get("role", "user"), "content": encoded[-budget:]})
            break
        selected.append(message)
        used += addition
    conversation = json.dumps(list(reversed(selected)), default=str, ensure_ascii=False)
    prompt = (
        _structured_summary_instruction()
        + f"\n[Conversation trimmed to last {budget} chars for summarization]\n\n"
        + conversation
    )
    try:
        response = create_message(
            model_id=get_model(),
            messages=[{"role": "user", "content": prompt}],
            # Reasoning models spend many tokens on thinking; 2000 often leaves
            # the visible text empty. 8000 gives room for both.
            max_tokens=8000,
        )
        text = extract_summary_text(response.content)
        if not text:
            return SUMMARY_UNUSABLE
        return text
    except Exception as exc:
        return (
            f"[Compact summary unavailable: {exc}]\n"
            "Earlier conversation was trimmed to fit the model input limit. "
            "Re-issue your last instruction if the agent seems to have lost context."
        )
