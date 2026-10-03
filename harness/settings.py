"""Runtime paths, LLM client, and harness constants."""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PACKAGE_ROOT / ".env")
load_dotenv(override=True)

BUILTIN_SKILLS_DIR = PACKAGE_ROOT / "skills"
BUILTIN_CONFIG_DIR = PACKAGE_ROOT / "config"
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
    # 统一目录：所有运行时系统数据都归属该工作区自己的 ``.project``，
    # 不再在工作区根目录平铺；``project_dir`` 自身保持 ``<root>/.project``
    # 不变，避免 ``.project/.project`` 嵌套。
    project = root / ".project"
    return WorkspacePaths(
        root=root,
        transcript_dir=project / ".transcripts",
        tool_results_dir=project / ".task_outputs" / "tool-results",
        tasks_dir=project / ".tasks",
        worktrees_dir=project / ".worktrees",
        mailbox_dir=project / ".mailboxes",
        memory_dir=project / ".memory",
        memory_index=project / ".memory" / "MEMORY.md",
        project_dir=project,
        durable_cron_path=project / ".scheduled_tasks.json",
        features_dir=project / ".features",
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

# ``PROJECT_DIR`` 先建（其余派生目录位于其下），再用 ``parents=True`` 兜底。
for path in (PROJECT_DIR, TASKS_DIR, WORKTREES_DIR, MAILBOX_DIR):
    path.mkdir(parents=True, exist_ok=True)


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


#: Harness metadata directory names that must never become the active
#: workspace root.  Opening one (e.g. ``/open repo/.project``) used to create
#: a nested ``.project/.project`` and trip the Goal ``workspace_changed``
#: guard, killing a running Goal 20 ms into its next round.
_RESERVED_WORKSPACE_NAMES = frozenset({
    ".project", ".tasks", ".worktrees", ".mailboxes", ".features",
    ".transcripts", ".task_outputs", ".memory",
})


def _discard_publish_staging(staging: Path) -> None:
    """Remove a staging replica; best-effort so cleanup never blocks migration."""
    try:
        if staging.exists():
            staging.unlink()
    except OSError:
        pass


def _migrate_legacy_tasks(root: Path, unified_tasks: Path) -> list[str]:
    """Publish legacy flat ``<root>/.tasks/*.json`` tasks into *unified_tasks*.

    Existence is judged per item, not per directory: a missing unified target is
    copied as a full replica, an existing target is left untouched (no override),
    and a per-item failure is isolated so remaining items still publish.  Legacy
    copies stay in place (copy, not move).  Returns failure strings (empty when
    everything published).

    Each item is first fully replicated under a staging name that deliberately
    differs from the final name, then published with a rename.  A failure while
    forming the replica can therefore never leave a partial file under the
    final unified name; the staging residue is discarded and the legacy item
    stays readable from its old location until a later retry publishes it.
    """
    legacy = root / ".tasks"
    if not legacy.is_dir():
        return []
    failures: list[str] = []
    for src in sorted(legacy.glob("*.json")):
        dst = unified_tasks / src.name
        if dst.exists():
            continue
        staging = unified_tasks / f".{src.name}.{uuid.uuid4().hex}.staging"
        try:
            shutil.copy2(src, staging)
            # 无覆盖发布：目标一旦已存在（并发/重试竞态）就保留既有统一目录项。
            if dst.exists():
                _discard_publish_staging(staging)
                continue
            os.rename(staging, dst)
        except Exception as exc:  # noqa: BLE001 - 单项失败不得阻断其他项目
            failures.append(f"{src.name}: {exc}")
            _discard_publish_staging(staging)
    return failures


def switch_workspace(root: Path) -> int:
    """Atomically switch the active workspace root.

    Returns the new generation.  Callers are responsible for refreshing any
    state that depends on the derived paths (sessions, RAG, tasks).
    """
    global _workspace, _workspace_generation
    root = root.expanduser().resolve()
    if root.name.lower() in _RESERVED_WORKSPACE_NAMES:
        raise ValueError(
            f"refusing to bind the workspace to harness metadata directory: {root}"
        )
    with _WORKSPACE_LOCK:
        _workspace = root
        _workspace_generation += 1
        # 初始化只创建统一目录（<root>/.project）及其内部运行时子目录，
        # 不再在根目录创建平铺数据目录；切换仅更新进程内活跃路径。
        paths = _derive_paths(root)
        for path in (
            paths.project_dir,
            paths.tasks_dir,
            paths.worktrees_dir,
            paths.mailbox_dir,
            paths.features_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # 逐项迁移旧式根目录平铺任务（<root>/.tasks）到统一目录。单项发布失败
        # 不阻断工作区切换：失败项保持未发布、经任务入口回退读取兜底；已发布项
        # 与旧位置副本均不受影响。
        _migrate_legacy_tasks(root, paths.tasks_dir)
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
# Permission prompts require an explicit human response. A positive timeout is
# fail-closed; zero means wait until a reply or cancellation arrives.
def _permission_timeout() -> float:
    raw = os.getenv("HARNESS_PERMISSION_TIMEOUT", "0").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return max(0.0, value)
PERMISSION_AUTO_APPROVE_TIMEOUT = _permission_timeout()


# Skills catalog directory: project-level `skills/` next to the workspace root
# (same layout as the built-in worktree skills and Claude Code's skills dir).
SKILLS_DIR = BUILTIN_SKILLS_DIR


def get_project_config_dir() -> Path:
    """Return the active workspace's project configuration directory."""
    return get_workdir() / "config"


def get_mcp_config_path() -> Path:
    project = get_project_config_dir() / "mcp.json"
    return project if project.exists() else BUILTIN_CONFIG_DIR / "mcp.json"


def get_permissions_config_path() -> Path:
    project = get_project_config_dir() / "permissions.json"
    return project if project.exists() else BUILTIN_CONFIG_DIR / "permissions.json"


def get_global_skills_dir() -> Path:
    return Path.home() / ".harness" / "skills"


def get_project_skills_dir() -> Path:
    return get_workdir() / "skills"
