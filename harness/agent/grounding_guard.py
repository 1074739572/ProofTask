"""Hard guard: block first-batch tools when deixis is unresolved and no Working goal text."""

from __future__ import annotations

import re

from harness.agent.compact.messages import find_latest_user_text
from harness.messages.blocks import block_text, is_text, is_tool_use

# Strong deixis — prefer miss over false positive (weak EN "it/this" alone not enough).
_DEIXIS_RE = re.compile(
    r"("
    r"这个|那个|这些|那些|上面|下面|刚才|此前|这里|那里|"
    r"上述|前述|该文档|该文件|这份|那份|"
    r"\bthis one\b|\bthat one\b|\babove\b|\bthe former\b|\bthe latter\b"
    r")",
    re.IGNORECASE,
)

# Concrete enough that we should not block even if a deixis marker appears.
_CONCRETE_RE = re.compile(
    r"("
    r"[A-Za-z0-9_\-./\\]+\.(py|md|ts|tsx|js|json|yaml|yml|toml|txt|docx)|"
    r"`[^`]+`|"
    r":\d+\b|"
    r"/[A-Za-z0-9_\-./]+|"
    r"[A-Za-z]:\\[^\s]+"
    r")"
)

_LOOKUP_CONSTRAINT_MARKER = "[Lookup mode — auto]"

BLOCK_MESSAGE = (
    "[GroundingGuard] Blocked: the latest user request has unresolved deixis "
    "(这/那/上面/刚才/this one/…) and this turn called tools without stating a "
    "resolved Working goal in text.\n"
    "Either (1) rewrite the goal from conversation context in one short sentence "
    "then call tools, or (2) ask the user 1–3 blocking clarifying questions with "
    "no tools (细节级 prep does not substitute for resolving deixis). "
    "Do not put assumptions or caveats inside tool parameters."
)


def strip_lookup_constraint(text: str) -> str:
    """User-facing query without the auto-appended lookup constraint block."""
    if _LOOKUP_CONSTRAINT_MARKER not in text:
        return text
    return text.split(_LOOKUP_CONSTRAINT_MARKER, 1)[0].rstrip()


def has_strong_deixis(text: str) -> bool:
    body = strip_lookup_constraint(text or "")
    return bool(_DEIXIS_RE.search(body))


def looks_concrete(text: str) -> bool:
    """True for path/line-anchored requests — do not block these."""
    body = strip_lookup_constraint(text or "")
    return bool(_CONCRETE_RE.search(body))


def response_has_intent_text(content) -> bool:
    if not content:
        return False
    for block in content:
        if is_text(block) and block_text(block).strip():
            return True
    return False


def response_has_tool_use(content) -> bool:
    if not content:
        return False
    return any(is_tool_use(block) for block in content)


class GroundingGuard:
    """Armed for the first tool-bearing assistant turn of an agent_loop call."""

    def __init__(self) -> None:
        self._armed = True

    @property
    def armed(self) -> bool:
        return self._armed

    def evaluate(self, messages: list, response_content) -> tuple[bool, str]:
        """Return (should_block_all_tools, message) for this assistant turn.

        Only the first tool batch after loop start is eligible. Prefers false
        negatives: no deixis / concrete path / intent text present → allow.
        """
        if not self._armed:
            return False, ""
        if not response_has_tool_use(response_content):
            return False, ""

        # First tool-bearing response consumes the arm either way.
        self._armed = False

        user_text = find_latest_user_text(messages) or ""
        if not has_strong_deixis(user_text):
            return False, ""
        if looks_concrete(user_text):
            return False, ""
        if response_has_intent_text(response_content):
            return False, ""
        return True, BLOCK_MESSAGE

    def block_message(self) -> str:
        return BLOCK_MESSAGE
