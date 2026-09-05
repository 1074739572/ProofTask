"""Skill catalog scanning and on-demand loading."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import yaml

from harness import settings

SKILL_REGISTRY: dict[str, dict] = {}
SKILL_CONFLICTS: dict[str, list[dict]] = {}
_REGISTRY_LOCK = threading.RLock()
_registry_generation = -1
MAX_SKILL_BODY_BYTES = 8_000
MAX_SKILL_DESCRIPTION_CHARS = 1_024

# User-role injection when the human forces a skill into the session ( /skill ).
SKILL_LOADED_PREFIX = "[Skill loaded:"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def skill_roots() -> list[tuple[str, Path]]:
    """Return skill roots from lowest to highest precedence."""
    roots = [("builtin", settings.BUILTIN_SKILLS_DIR), ("global", settings.get_global_skills_dir()), ("project", settings.get_project_skills_dir())]
    seen: set[Path] = set()
    result = []
    for source, root in roots:
        root = root.resolve()
        if root not in seen:
            result.append((source, root))
            seen.add(root)
    return result


def scan_skills() -> None:
    global _registry_generation
    fresh: dict[str, dict] = {}
    conflicts: dict[str, list[dict]] = {}
    for source, root in skill_roots():
        if not root.exists():
            continue
        try:
            directories = sorted(root.iterdir())
        except OSError:
            continue
        for directory in directories:
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            try:
                raw = manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            meta, _ = _parse_frontmatter(raw)
            name = str(meta.get("name", directory.name)).strip()
            if not name:
                continue
            desc = str(meta.get("description", raw.split("\n")[0].lstrip("#").strip()))
            entry = {
                "name": name,
                "description": desc[:MAX_SKILL_DESCRIPTION_CHARS],
                "content": raw,
                "path": str(manifest),
                "source": source,
            }
            if name in fresh:
                conflicts.setdefault(name, [fresh[name]])
                conflicts[name].append(entry)
            fresh[name] = entry
    with _REGISTRY_LOCK:
        SKILL_REGISTRY.clear()
        SKILL_REGISTRY.update(fresh)
        SKILL_CONFLICTS.clear()
        SKILL_CONFLICTS.update(conflicts)
        _registry_generation = settings.workspace_generation()


def list_skills() -> str:
    if _registry_generation != settings.workspace_generation():
        scan_skills()
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- {skill['name']}: {skill['description']}"
        for skill in SKILL_REGISTRY.values()
    )


def skill_names() -> list[str]:
    """Sorted skill ids for pickers."""
    if _registry_generation != settings.workspace_generation():
        scan_skills()
    return sorted(SKILL_REGISTRY.keys())


def load_skill(name: str) -> str:
    """Load a bounded skill body for model context.

    Use :func:`load_skill_detail` when callers need provenance/truncation
    metadata.  The string API remains for backwards compatibility.
    """
    detail = load_skill_detail(name)
    return detail["content"]


def load_skill_detail(name: str) -> dict:
    """Return skill content plus provenance and truncation metadata."""
    if _registry_generation != settings.workspace_generation():
        scan_skills()
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return {
            "name": name,
            "content": f"Skill not found: {name}. Available: {available}",
            "source": "",
            "path": "",
            "truncated": False,
            "sha256": "",
        }
    content = skill["content"]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    truncated = False
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SKILL_BODY_BYTES:
        content = encoded[:MAX_SKILL_BODY_BYTES].decode("utf-8", errors="ignore")
        content += "\n\n[skill body truncated; read the source file for the remainder]"
        truncated = True
    return {
        "name": skill["name"],
        "content": content,
        "source": skill.get("source", ""),
        "path": skill.get("path", ""),
        "truncated": truncated,
        "sha256": digest,
    }


def format_skill_injection(name: str, content: str) -> str:
    return f"{SKILL_LOADED_PREFIX} {name}]\n{content}"


def skill_loaded_notice(name: str) -> str:
    return f"已加载 skill: {name}"


def parse_skill_loaded_name(text: str) -> str | None:
    """Extract skill name from an injection message (first line)."""
    if not isinstance(text, str):
        return None
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first.startswith(SKILL_LOADED_PREFIX):
        return None
    rest = first[len(SKILL_LOADED_PREFIX) :].strip()
    if rest.endswith("]"):
        rest = rest[:-1].strip()
    return rest or None


def is_skill_injection(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    return content.strip().startswith(SKILL_LOADED_PREFIX)


def format_skill_command_status() -> str:
    """Human list for `/skill` with no args."""
    scan_skills()
    if not SKILL_REGISTRY:
        return "（暂无 skill）\n用法：把 skill 放在 skills/<name>/SKILL.md\n加载：/skill <name>"
    lines = [
        "Skills",
        "用法：/skill <name> 将全文注入当前会话  ·  加载后再提问",
    ]
    if SKILL_CONFLICTS:
        lines.append("警告：存在同名 Skill 覆盖（project/global 优先级更高）：")
        for name, entries in sorted(SKILL_CONFLICTS.items()):
            lines.append(f"  - {name}: " + ", ".join(str(item.get("source", "?")) for item in entries))
    for skill in SKILL_REGISTRY.values():
        desc = (skill.get("description") or "").strip()
        if len(desc) > 80:
            desc = desc[:79] + "…"
        lines.append(f"  - {skill['name']}: {desc}")
    return "\n".join(lines)


def inject_skill(
    name: str,
    messages: list,
    *,
    checkpoint: bool = True,
    binding = None,
) -> tuple[bool, str]:
    """Append a marked user message with full skill body and optionally checkpoint."""
    scan_skills()
    raw = (name or "").strip()
    if not raw:
        return False, "用法：/skill <name>  ·  /skill 查看列表"
    if raw not in SKILL_REGISTRY:
        available = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return False, f"Skill not found: {raw}. Available: {available}"

    # Loading the same immutable skill twice only inflates history and can
    # cause contradictory duplicate instructions after a later edit.  Treat a
    # matching injection as idempotent; callers can restart the session or
    # explicitly reload after changing the file.
    for message in messages:
        if is_skill_injection(message) and parse_skill_loaded_name(message.get("content", "")) == raw:
            return True, skill_loaded_notice(raw) + "（已在当前会话加载）"

    content = load_skill(raw)
    messages.append({"role": "user", "content": format_skill_injection(raw, content)})
    if checkpoint:
        from harness.project.resume import checkpoint_history

        checkpoint_history(messages, binding=binding)
    return True, skill_loaded_notice(raw)


def run_skill_command(args: str = "", *, messages: list | None = None, binding = None) -> str:
    """Handle /skill [name|list]."""
    raw = (args or "").strip()
    sub = raw.lower()
    if sub in ("", "list", "status", "ls", "pick", "picker"):
        return format_skill_command_status()
    if messages is None:
        return "请在 CLI 中执行 /skill <name>"
    _ok, note = inject_skill(raw, messages, checkpoint=True, binding=binding)
    return note


scan_skills()
