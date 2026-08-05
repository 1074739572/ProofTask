"""Terminal output helpers for CLI and background threads."""

from __future__ import annotations

from harness import terminal_state


def terminal_print(text: str) -> None:
    from harness.ui.renderer import renderer

    renderer.assistant(text)


CLI_ACTIVE = terminal_state.CLI_ACTIVE
