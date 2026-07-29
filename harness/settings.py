"""Runtime paths, LLM client, and harness constants."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path.cwd()

load_dotenv(PACKAGE_ROOT / ".env")
load_dotenv(override=True)

SKILLS_DIR = PACKAGE_ROOT / "skills"
CONFIG_DIR = PACKAGE_ROOT / "config"
MCP_CONFIG_PATH = CONFIG_DIR / "mcp.json"
PROVIDERS_CONFIG_PATH = CONFIG_DIR / "providers.json"
MODELS_CONFIG_PATH = CONFIG_DIR / "models.json"

TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
TASKS_DIR = WORKDIR / ".tasks"
WORKTREES_DIR = WORKDIR / ".worktrees"
MAILBOX_DIR = WORKDIR / ".mailboxes"
MEMORY_DIR = WORKDIR / ".memory"
PROJECT_DIR = WORKDIR / ".project"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
DURABLE_CRON_PATH = WORKDIR / ".scheduled_tasks.json"

for path in (TASKS_DIR, WORKTREES_DIR, MAILBOX_DIR, PROJECT_DIR):
    path.mkdir(exist_ok=True)

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
