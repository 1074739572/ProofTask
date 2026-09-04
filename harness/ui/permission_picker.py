"""Interactive ``/permission`` mode picker for the classic terminal."""

from __future__ import annotations

from harness.permission_session import PERMISSION_MODES, get_permission_mode, set_permission_mode
from harness.ui.terminal_menu import is_interactive_tty, select_from_list


_LABELS = {
    "default": "默认权限（低风险自动放行）",
    "auto-review": "自动审查（低、中风险自动放行）",
    "full-access": "完全访问（低、中、高风险自动放行）",
}


def menu_entries() -> tuple[list[str], list[str], int]:
    current = get_permission_mode()
    labels: list[str] = []
    modes = list(PERMISSION_MODES)
    cursor = 0
    for index, mode in enumerate(modes):
        suffix = " · 当前" if mode == current else ""
        labels.append(f"{_LABELS.get(mode, mode)} [{mode}]{suffix}")
        if mode == current:
            cursor = index
    return labels, modes, cursor


def run_permission_picker() -> str:
    """Show the three session modes and apply the selected one."""
    if not is_interactive_tty():
        return (
            f"Permission mode: {get_permission_mode()}\n"
            "Available modes: default, auto-review, full-access\n"
            "Usage: /permission <mode>"
        )
    labels, modes, cursor = menu_entries()
    choice = select_from_list(
        labels,
        title="Select permission mode",
        initial_index=cursor,
        hint="↑↓ move · Enter confirm · Esc cancel",
    )
    if choice is None:
        return f"Kept permission mode: {get_permission_mode()}"
    return f"Permission mode set to: {set_permission_mode(modes[choice])}"


__all__ = ["menu_entries", "run_permission_picker"]
