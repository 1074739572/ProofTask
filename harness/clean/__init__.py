"""Clean-state and handover checks (L4).

Triggers: task completion (``complete_task``), explicit ``/check``-style calls,
session exit. NOT a per-turn ``Stop`` hook (see the reliability plan §0.3).

Modes (``HARNESS_CLEAN_MODE``): ``off`` / ``warn`` (default) / ``enforce``.
In ``enforce``, hard failures (temp artifacts, feature/evidence inconsistency)
block task completion; soft info (uncommitted git changes) never blocks.

Public API::

    run_clean_check(workspace=None, *, mode=None) -> CleanReport
    clean_mode() -> str            # env HARNESS_CLEAN_MODE
    CleanReport / CleanCheck
"""

from harness.clean.checker import (
    CLEAN_MODES,
    DEFAULT_CLEAN_MODE,
    ENV_CLEAN_MODE,
    HARD_CHECKS,
    CleanCheck,
    CleanReport,
    clean_mode,
    run_clean_check,
)

__all__ = [
    "CLEAN_MODES",
    "DEFAULT_CLEAN_MODE",
    "ENV_CLEAN_MODE",
    "HARD_CHECKS",
    "CleanCheck",
    "CleanReport",
    "clean_mode",
    "run_clean_check",
]
