"""Runtime mode selection (config-driven)."""

from __future__ import annotations

import os
import re
import threading

from harness.modes.registry import (
    default_mode_id,
    format_mode_catalog,
    get_mode_profile,
    list_mode_ids,
)

_lock = threading.Lock()
_env_mode = os.getenv("HARNESS_MODE", "").strip().lower()
_current_mode: str = _env_mode if _env_mode else default_mode_id()
if _current_mode not in list_mode_ids():
    _current_mode = default_mode_id()

# Grill / confirm-before-execute: tools stay locked until the user confirms.
_execute_unlocked = False

_CONFIRM_RE = re.compile(
    r"(?:"
    r"确认执行|可以开始|开始执行|开始吧|就这么做|就这样执行|定了\s*开始|"
    r"同意执行|按这个做|按此执行|"
    r"\bgo(?:\s+ahead)?\b|\bproceed\b|\bapproved?\b|\blgtm\b|"
    r"\bexecute\b|\bship\s+it\b|\bdo\s+it\b"
    r")",
    re.IGNORECASE,
)

_RELOCK_RE = re.compile(
    r"(?:"
    r"重新拷问|重新确认|先别执行|先不要执行|回到拷问|再拷问|"
    r"\bre-?grill\b|\block\b|\bhold\b|\bdon'?t\s+execute\b"
    r")",
    re.IGNORECASE,
)


def get_mode() -> str:
    with _lock:
        return _current_mode


def get_current_mode_profile():
    profile = get_mode_profile(get_mode())
    if profile is None:
        return get_mode_profile(default_mode_id())
    return profile


def is_execute_unlocked() -> bool:
    with _lock:
        return _execute_unlocked


def set_execute_unlocked(unlocked: bool) -> None:
    global _execute_unlocked
    with _lock:
        _execute_unlocked = bool(unlocked)


def looks_like_execute_confirm(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    # Short affirmations alone are too weak to unlock irreversible work.
    if len(raw) <= 2 and raw in {"嗯", "好", "行", "ok", "OK"}:
        return False
    return bool(_CONFIRM_RE.search(raw))


def looks_like_execute_relock(text: str) -> bool:
    return bool(_RELOCK_RE.search((text or "").strip()))


def note_user_query_for_mode(query: str) -> str | None:
    """Update confirm-before-execute lock from the latest user message.

    Returns an optional short UI status string when the lock state changes.
    """
    profile = get_current_mode_profile()
    if profile is None or not profile.confirm_before_execute:
        return None
    if looks_like_execute_relock(query):
        was = is_execute_unlocked()
        set_execute_unlocked(False)
        return "Grill locked — grilling only until you confirm again" if was else None
    if looks_like_execute_confirm(query):
        was = is_execute_unlocked()
        set_execute_unlocked(True)
        return "Grill unlocked — executing the agreed plan" if not was else None
    return None


def set_mode(mode: str) -> str:
    global _current_mode
    mode = mode.strip().lower()
    if mode not in list_mode_ids():
        return f"Unknown mode '{mode}'.\n\n{format_mode_catalog()}"
    previous = get_mode()
    with _lock:
        _current_mode = mode
    # Always re-lock when entering (or re-entering) a confirm-gated mode.
    profile = get_mode_profile(mode)
    if profile and profile.confirm_before_execute:
        set_execute_unlocked(False)
    elif previous != mode:
        set_execute_unlocked(False)
    if mode == "file" and previous != "file":
        from harness.rag.file_mode import on_enter_file_mode

        return on_enter_file_mode()
    if profile and profile.confirm_before_execute:
        return format_grill_mode_banner(profile)
    return format_mode_status()


def format_grill_mode_banner(profile=None) -> str:
    profile = profile or get_current_mode_profile()
    skills = ", ".join(profile.builtin_skills) if profile and profile.builtin_skills else "grill-me"
    lock = (
        "当前：🔓 已解锁（可按共识执行）"
        if is_execute_unlocked()
        else "当前：🔒 已锁定（只读分析 + 提问，不可改文件/跑破坏性命令）"
    )
    return "\n".join(
        [
            f"Mode: {profile.id} — {profile.label}" if profile else "Mode: grill",
            profile.summary if profile and profile.summary else "",
            f"内置 skill: {skills}",
            "流程：提出目标 → 逐题拷问澄清 → 你回复「确认执行」/ go 后再动手",
            "重新上锁：说「重新拷问」或切换出再进 /mode grill",
            lock,
        ]
    ).strip()


def format_mode_status() -> str:
    profile = get_current_mode_profile()
    if profile is None:
        return f"Mode: {get_mode()}"
    if profile.confirm_before_execute:
        return format_grill_mode_banner(profile)
    parts = [f"Mode: {profile.id} — {profile.label}"]
    if profile.summary:
        parts.append(profile.summary)
    if profile.id == "file":
        from harness.rag.file_mode import format_file_mode_banner

        return format_file_mode_banner()
    if profile.enable_task:
        parts.append("task() enabled → see config/agents.json")
    if profile.lead_model_hint:
        label = "bound lead model" if profile.enable_task else "recommended lead /model"
        parts.append(f"{label}: {profile.lead_model_hint}")
    if profile.builtin_skills:
        parts.append(f"builtin skills: {', '.join(profile.builtin_skills)}")
    return "\n".join(parts)


def mode_prompt_section() -> str:
    profile = get_current_mode_profile()
    if profile is None:
        return ""
    text = profile.prompt
    if profile.id == "lookup":
        text += (
            "\n\nRuntime note: LookupGuard is not installed in this build. "
            "Do not claim a hidden guard will block requests; use the supplied "
            "tool permissions and RepeatGuard, then stop once the evidence is sufficient."
        )
    if profile.confirm_before_execute:
        if is_execute_unlocked():
            text += (
                "\n\nExecution gate: UNLOCKED\n"
                "- The user confirmed. Execute the agreed plan now.\n"
                "- Do not reopen settled decisions unless new evidence appears.\n"
                "- If they start a clearly new goal, ask them to re-confirm "
                "or say「重新拷问」."
            )
        else:
            text += (
                "\n\nExecution gate: LOCKED\n"
                "- Mutating tools are disabled until the user confirms.\n"
                "- Grill one question at a time; look up facts with read tools.\n"
                "- When aligned, summarize and ask them to reply "
                "「确认执行」 or go."
            )
    return text


def mode_builtin_skills_section() -> str:
    """Inject mode-bundled skill bodies into ephemeral session context."""
    profile = get_current_mode_profile()
    if profile is None or not profile.builtin_skills:
        return ""
    from harness.skills_loader import load_skill, scan_skills

    scan_skills()
    blocks: list[str] = []
    for name in profile.builtin_skills:
        content = load_skill(name)
        if content.startswith("Skill not found:"):
            blocks.append(f"[Builtin skill missing: {name}]")
            continue
        blocks.append(f"[Builtin skill: {name}]\n{content}")
    return "\n\n".join(blocks)


def mode_disables_tool(tool_name: str) -> bool:
    profile = get_current_mode_profile()
    if profile is None:
        return False
    if profile.allowed_tools is not None and not (
        profile.confirm_before_execute and is_execute_unlocked()
    ):
        allowed = profile.allowed_tools
        wildcard_allowed = any(
            tool_name.startswith(pattern[:-1])
            for pattern in allowed
            if pattern.endswith("*")
        )
        if tool_name not in allowed and not wildcard_allowed:
            return True
    disabled = profile.disable_tools
    if tool_name not in disabled and not (
        tool_name.startswith("mcp__") and "mcp__*" in disabled
    ):
        return False
    # Confirm-gated modes list locked tools in disable_tools; unlock clears them.
    if profile.confirm_before_execute and is_execute_unlocked():
        return False
    return True


def mode_enables_task() -> bool:
    profile = get_current_mode_profile()
    if profile is None:
        return False
    if profile.confirm_before_execute and not is_execute_unlocked():
        return False
    return profile.enable_task


def mode_auto_route() -> bool:
    """Whether the system should auto-route user messages to sub-agents."""
    profile = get_current_mode_profile()
    if profile is None:
        return False
    if profile.confirm_before_execute and not is_execute_unlocked():
        return False
    return profile.auto_route


def mode_lead_model_hint() -> str | None:
    profile = get_current_mode_profile()
    if profile is None:
        return None
    return profile.lead_model_hint
