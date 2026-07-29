"""Interactive /model picker (↑↓ + Enter), Cursor/Codex-style."""

from __future__ import annotations

from harness.models import format_model_list, get_model, list_models, set_model
from harness.providers.config import get_provider, provider_key_status
from harness.ui.terminal_menu import is_interactive_tty, select_from_list


def menu_entries() -> tuple[list[str], list[str], int]:
    """Labels, model ids, and initial cursor index for CLI pickers."""
    current = get_model()
    key_status = provider_key_status()
    labels: list[str] = []
    model_ids: list[str] = []
    cursor_index = 0

    for index, entry in enumerate(list_models()):
        model_id = entry["id"]
        model_ids.append(model_id)
        if model_id == current:
            cursor_index = index

        label = entry.get("label", model_id)
        provider = entry.get("provider", "deepseek")
        try:
            provider_note = get_provider(provider).label
        except KeyError:
            provider_note = provider

        key_ok = key_status.get(provider, False)
        key_tag = "" if key_ok else " · no key"
        current_tag = " · current" if model_id == current else ""
        line = f"{label}  [{provider_note}]{current_tag}{key_tag}"
        labels.append(line)

    return labels, model_ids, cursor_index


def _menu_entries() -> tuple[list[str], list[str], int]:
    return menu_entries()


def run_model_picker() -> str:
    """Show model list; use arrow keys + Enter when in an interactive terminal."""
    if not is_interactive_tty():
        return format_model_list() + "\n\nSwitch with: /model <id>"

    labels, model_ids, cursor_index = menu_entries()
    if not model_ids:
        return "No models configured in config/models.json"

    try:
        choice = select_from_list(
            labels,
            title="Select model",
            initial_index=cursor_index,
        )
    except KeyboardInterrupt:
        return f"Kept current model: {get_model()}"

    if choice is None:
        return f"Kept current model: {get_model()}"

    return set_model(model_ids[choice])
