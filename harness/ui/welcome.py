"""Startup welcome screen — centered smiley + daily quote."""

from __future__ import annotations

from harness.ui import theme
from harness.ui.banner import SMILEY
from harness.ui.quotes import get_daily_quote, maybe_refill_async

try:
    from rich.console import Console
    from rich.text import Text
    from rich.align import Align
    from rich.style import Style

    _RICH = True
except ImportError:
    _RICH = False


def _block_smiley() -> Text:
    face = Text()
    for i, row in enumerate(SMILEY):
        styled = Text(row + "\n")
        if i == 0 or i == len(SMILEY) - 1:
            styled.stylize(f"bold {theme.ACCENT}")
        elif "██████" in row:
            styled.stylize("bold red")
        elif "██    ██" in row or row.count("██") >= 4:
            styled.stylize("bold yellow")
        else:
            styled.stylize(f"bold {theme.ACCENT}")
        face.append_text(styled)
    return face


def render_welcome(*, session_source: str | None = None) -> None:
    """Print centered smiley + daily quote (colored, no borders)."""
    maybe_refill_async()
    quote = get_daily_quote()

    if not _RICH:
        print()
        print("  (:  :)")
        print()
        if quote:
            print(f"  {quote}")
        print()
        return

    console = Console(highlight=False, legacy_windows=False)
    width = console.size.width

    console.print()
    console.print(Align.center(_block_smiley()))
    console.print()

    if quote:
        styled = Text()
        styled.append("\n")
        styled.append(
            f"「{quote}」",
            style=Style(color="bright_yellow", bold=True),
        )
        styled.append("\n\n")
        console.print(Align.center(styled), justify="center")
        console.print()
