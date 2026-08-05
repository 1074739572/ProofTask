"""Runtime paths, LLM client, and harness constants."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PACKAGE_ROOT / ".env")
load_dotenv(override=True)

SKILLS_DIR = PACKAGE_ROOT / "skills"
CONFIG_DIR = PACKAGE_ROOT / "config"
MCP_CONFIG_PATH = CONFIG_DIR / "mcp.json"
PERMISSIONS_CONFIG_PATH = CONFIG_DIR / "permissions.json"
PROVIDERS_CONFIG_PATH = CONFIG_DIR / "providers.json"
MODELS_CONFIG_PATH = CONFIG_DIR / "models.json"

# The process workspace.  ``WORKDIR`` is the startup directory (frozen at import
# time, historically ``Path.cwd()``); the *active* workspace is tracked in
# ``WorkspaceContext`` so a running process can switch projects without
# restarting (opencode-style directory-bound sessions).  Everything that must
# follow the current project reads ``get_workdir()``.
WORKDIR = Path.cwd()

_WORKSPACE_LOCK = threading.RLock()
_workspace = WORKDIR
_workspace_generation = 0


@dataclass(frozen=True)
class WorkspacePaths:
    """Paths derived from a workspace root (recomputed on every switch)."""

    root: Path
    transcript_dir: Path
    tool_results_dir: Path
    tasks_dir: Path
    worktrees_dir: Path
    mailbox_dir: Path
    memory_dir: Path
    memory_index: Path
    project_dir: Path
    durable_cron_path: Path
    features_dir: Path


def _derive_paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root,
        transcript_dir=root / ".transcripts",
        tool_results_dir=root / ".task_outputs" / "tool-results",
        tasks_dir=root / ".tasks",
        worktrees_dir=root / ".worktrees",
        mailbox_dir=root / ".mailboxes",
        memory_dir=root / ".memory",
        memory_index=root / ".memory" / "MEMORY.md",
        project_dir=root / ".project",
        durable_cron_path=root / ".scheduled_tasks.json",
        features_dir=root / ".features",
    )


# Module-level mirrors of the startup workspace.  These are kept for
# backward-compatible imports; code that must follow live switches should use
# ``get_workspace()`` / ``get_workdir()`` instead.
_ws = _derive_paths(WORKDIR)

TRANSCRIPT_DIR = _ws.transcript_dir
TOOL_RESULTS_DIR = _ws.tool_results_dir
TASKS_DIR = _ws.tasks_dir
WORKTREES_DIR = _ws.worktrees_dir
MAILBOX_DIR = _ws.mailbox_dir
MEMORY_DIR = _ws.memory_dir
PROJECT_DIR = _ws.project_dir
MEMORY_INDEX = _ws.memory_index
DURABLE_CRON_PATH = _ws.durable_cron_path

for path in (TASKS_DIR, WORKTREES_DIR, MAILBOX_DIR, PROJECT_DIR):
    path.mkdir(exist_ok=True)


def get_workdir() -> Path:
    """Active workspace root (thread-safe, follows in-process switches)."""
    with _WORKSPACE_LOCK:
        return _workspace


def get_workspace_paths() -> WorkspacePaths:
    """Active derived paths (thread-safe)."""
    with _WORKSPACE_LOCK:
        return _derive_paths(_workspace)


def workspace_generation() -> int:
    """Increments on every workspace switch — caches keyed on it can invalidate."""
    with _WORKSPACE_LOCK:
        return _workspace_generation


def switch_workspace(root: Path) -> int:
    """Atomically switch the active workspace root.

    Returns the new generation.  Callers are responsible for refreshing any
    state that depends on the derived paths (sessions, RAG, tasks).
    """
    global _workspace, _workspace_generation
    root = root.expanduser().resolve()
    with _WORKSPACE_LOCK:
        _workspace = root
        _workspace_generation += 1
        for path in (root / ".tasks", root / ".worktrees", root / ".mailboxes", root / ".project", root / ".features"):
            path.mkdir(parents=True, exist_ok=True)
        return _workspace_generation

FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

from harness.models import initialize_model  # noqa: E402

initialize_model()

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
# Legacy name: auto-compact is now token-based (see context_limit()).
# Kept only as a documentation breadcrumb; do not use as a char budget.
CONTEXT_LIMIT = 50_000
KEEP_RECENT_TOOL_RESULTS = 3


def context_limit() -> int:
    """Token threshold before auto-compaction (Claude Code–style).

    Default: ``0.835 × model_context_window`` (see ``sizing.autocompact_threshold_tokens``).

    Env overrides:
    - ``HARNESS_AUTOCOMPACT_PCT`` — fraction (0.835) or percent (83.5)
    - ``HARNESS_CONTEXT_WINDOW`` — force model window in tokens
    - ``HARNESS_CONTEXT_LIMIT`` — absolute token threshold (bypasses pct × window)
    """
    from harness.agent.compact.sizing import autocompact_threshold_tokens

    return autocompact_threshold_tokens()


def compact_tail_count() -> int:
    """How many trailing messages survive a full compaction (default 5)."""
    raw = os.getenv("HARNESS_COMPACT_TAIL", "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = (
    "Continue from the previous response. Do not repeat completed work."
)
import sys

# Built-in input() must use a prompt encodable on the active console (GBK on Windows).
if sys.platform == "win32":
    CLI_PROMPT = "\033[36m> \033[0m"
else:
    CLI_PROMPT = "\033[36m› \033[0m"

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def _positive_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, value)


# Teammates check this deadline between model and tool calls. It is deliberately
# lower than the router's absolute wait so a cooperative timeout can be reported.
TEAMMATE_MAX_RUNTIME = _positive_seconds("HARNESS_TEAMMATE_MAX_RUNTIME", 540)
ROUTE_IDLE_TIMEOUT = _positive_seconds("HARNESS_ROUTE_IDLE_TIMEOUT", 180)
ROUTE_MAX_RUNTIME = _positive_seconds("HARNESS_ROUTE_MAX_RUNTIME", 600)

# Welcome hero variants for /banner demo: classic | emoji | typewriter | shadow3d
BANNER_STYLE = os.getenv("HARNESS_BANNER", "classic").strip().lower()

# Skills catalog directory: project-level `skills/` next to the workspace root
# (same layout as the built-in worktree skills and Claude Code's skills dir).
SKILLS_DIR = WORKDIR / "skills"

# Declarative config files under `config/`.
CONFIG_DIR = WORKDIR / "config"
MCP_CONFIG_PATH = CONFIG_DIR / "mcp.json"
PERMISSIONS_CONFIG_PATH = CONFIG_DIR / "permissions.json"
