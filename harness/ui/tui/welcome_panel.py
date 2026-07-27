"""TUI welcome: slim brand line + quote card + gradient rule (premium shell)."""

from __future__ import annotations

from dataclasses import dataclass

from harness.ui.banner import TAGLINE
from harness.ui.tui.quotes import get_daily_quote_item, maybe_refill_async

SMILEY_SLIM: str = "\n".join(
    [
        " ╭───╮ ",
        " │· ·│ ",
        " │ ‿ │ ",
        " ╰───╯ ",
    ]
)

HELLO_LABEL = "IMPROVED HARNESS"
NARROW_HELLO = "improved_harness"


@dataclass(frozen=True)
class WelcomeParts:
    wide: bool
    smiley: str
    hello_title: str
    tagline: str
    narrow: str
    quote_body: str
    quote_source: str


def format_quote_body(text: str, source: str = "") -> tuple[str, str]:
    """Return (body, source_line) for the quote card."""
    body = (text or "").strip()
    if body and not (body.startswith("「") or body.startswith('"') or body.startswith("“")):
        body = f"“{body}”"
    src = (source or "").strip()
    source_line = f"— {src}" if src and src != "fallback" else ""
    return body, source_line


def build_welcome_parts(*, wide: bool) -> WelcomeParts:
    maybe_refill_async()
    item = get_daily_quote_item()
    raw_text = item.get("hitokoto") or ""
    raw_source = item.get("from") or ""
    body, source_line = format_quote_body(raw_text, raw_source)
    # Fallback when quote API returns empty
    if not body:
        body = "“Write code, not wars.”"
        source_line = ""
    if wide:
        return WelcomeParts(
            wide=True,
            smiley=SMILEY_SLIM,
            hello_title=HELLO_LABEL,
            tagline=TAGLINE,
            narrow="",
            quote_body=body,
            quote_source=source_line,
        )
    return WelcomeParts(
        wide=False,
        smiley="",
        hello_title="",
        tagline=TAGLINE,
        narrow=f"{NARROW_HELLO}\n{TAGLINE}",
        quote_body=body,
        quote_source=source_line,
    )
