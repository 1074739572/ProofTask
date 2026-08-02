"""CLI query input with optional redo after interrupt."""

from __future__ import annotations

from harness.models import get_model
from harness.path_completion import ReadlinePathCompleter
from harness.terminal_state import READLINE_AVAILABLE


def format_cli_prompt() -> str:
    """Prompt that shows the active model (Claude Code status-line idea, lightweight)."""
    from harness.modes import get_mode

    model = get_model()
    mode = get_mode()
    # ASCII-safe for Windows GBK consoles; keep cyan like CLI_PROMPT.
    if mode == "file":
        return f"\033[36m[{model}|file] > \033[0m"
    return f"\033[36m[{model}] > \033[0m"


def _read_with_path_completion(prompt: str, *, redo: str | None = None) -> str:
    if not READLINE_AVAILABLE:
        return input(prompt)

    import readline

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(ReadlinePathCompleter(readline))
    readline.set_completer_delims("")
    readline.parse_and_bind("tab: complete")

    if redo:
        def _prefill() -> None:
            readline.insert_text(redo)

        readline.set_startup_hook(_prefill)
    try:
        return input(prompt)
    finally:
        if redo:
            readline.set_startup_hook()
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def read_cli_query(*, redo: str | None = None, prompt: str | None = None) -> str:
    """
    Read the next user query.

    When ``redo`` is set (after Esc/Ctrl+C), pre-fill the line when readline is
    available; otherwise empty Enter resends the previous text.
    """
    active_prompt = prompt if prompt is not None else format_cli_prompt()
    if not redo:
        return _read_with_path_completion(active_prompt)

    if READLINE_AVAILABLE:
        line = _read_with_path_completion(active_prompt, redo=redo)
        return line if line.strip() else redo

    print(f"\n  (previous question — press Enter to resend, or type a new one)\n  {redo}\n")
    line = input(active_prompt)
    return line if line.strip() else redo
