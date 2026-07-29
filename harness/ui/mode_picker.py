"""Interactive /mode picker."""

from __future__ import annotations

from harness.modes import format_mode_status, get_mode, set_mode
from harness.modes.registry import get_mode_profile, list_mode_ids
from harness.ui.terminal_menu import is_interactive_tty, select_from_list


def menu_entries() -> tuple[list[str], list[str], int]:
    """Labels, mode ids, and initial cursor for CLI pickers."""
    labels: list[str] = []
    mode_ids: list[str] = []
    current = get_mode()
    cursor = 0
    for index, mode_id in enumerate(list_mode_ids()):
        mode_ids.append(mode_id)
        profile = get_mode_profile(mode_id)
        if mode_id == current:
            cursor = index
        if profile is None:
            labels.append(mode_id)
            continue
        current_tag = " · current" if mode_id == current else ""
        summary = f" — {profile.summary}" if profile.summary else ""
        labels.append(f"{profile.label} ({mode_id}){summary}{current_tag}")
    return labels, mode_ids, cursor


def _menu_entries() -> tuple[list[str], list[str], int]:
    return menu_entries()


def run_mode_picker() -> str:
    if not is_interactive_tty():
        from harness.modes.registry import format_mode_catalog

        return format_mode_catalog()

    labels, mode_ids, cursor = menu_entries()
    if not mode_ids:
        return "No modes in config/modes.json"

    choice = select_from_list(
        labels,
        title="Select mode",
        initial_index=cursor,
        hint="↑↓ move · Enter confirm · Esc cancel",
    )
    if choice is None:
        return format_mode_status()
    return set_mode(mode_ids[choice])
