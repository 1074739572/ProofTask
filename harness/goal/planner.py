"""Goal decomposition planner (L6 v2).

Before execution starts, a read-only subagent analyzes the repository and the
goal target and proposes a feature plan: a list of features with behavior,
per-feature verification suggestions, and dependency edges (a DAG).

Design rules:

- the plan is advisory until validated: every per-feature verification command
  must pass ``check_verification_command`` (structural policy); anything that
  fails validation falls back to the user's full ``--verify`` command;
- parsing is fault-tolerant (pure function, mirroring evaluation/parser.py):
  malformed plans degrade to a single whole-goal feature rather than failing
  the goal;
- dependencies are referenced by feature *name* in the plan and resolved to
  ids after creation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.settings import get_workdir
from harness.verification.policy import check_verification_command

#: Read-only subagent used for planning (same large-context explorer).
PLANNER_AGENT = "explore"
#: Bounds on the plan size (runaway decomposition protection).
MIN_FEATURES = 1
MAX_FEATURES = 8
#: Cap on per-feature behavior text.
MAX_BEHAVIOR_CHARS = 600

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
#: run_agent_task prefixes results with `[agent / model] description (N tools, Xs)`.
_AGENT_HEADER = re.compile(r"^\[[^\]]+\] [^\n]*\n*")


def _strip_agent_header(text: str) -> str:
    stripped = text.lstrip()
    if _AGENT_HEADER.match(stripped):
        stripped = _AGENT_HEADER.sub("", stripped, count=1)
    return stripped.strip()


@dataclass(frozen=True)
class FeaturePlan:
    name: str
    behavior: str
    verification: str = ""  # "" = fall back to the full verification command
    depends_on: tuple[str, ...] = ()  # feature *names* this feature depends on

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "behavior": self.behavior,
            "verification": self.verification,
            "depends_on": list(self.depends_on),
        }


def build_plan_prompt(target: str, full_verification: str) -> str:
    """Prompt for the read-only planning subagent (output: JSON array)."""
    return (
        "You are planning the decomposition of one goal into verifiable features.\n\n"
        f"Goal target: {target}\n"
        f"Full verification command (authoritative, read-only): {full_verification}\n\n"
        "Explore the repository with your read-only tools, then split the goal "
        f"into {MIN_FEATURES}–{MAX_FEATURES} features:\n"
        "- Each feature must be independently implementable and verifiable.\n"
        "- Prefer the order that an engineer would implement them in.\n"
        "- ``depends_on`` lists the NAMES of features that must be done first "
        "(empty array when none).\n"
        "- ``verification``: a concrete read-only command that proves this "
        "feature works (e.g. ``pytest tests/test_x.py -q`` or ``python "
        "tools/check_x.py``). It must be a single command, no shell "
        "metacharacters (&& ; | > <), no ``python -c``, no destructive "
        "tokens. If you cannot find a real command in the repo for a feature, "
        "use an empty string (the full verification will cover it).\n"
        "- If the goal is small or cannot be split meaningfully, return a "
        "single feature.\n"
        "- IMPORTANT: when the goal text explicitly enumerates independent "
        "deliverables (multiple modules / functions / numbered sub-tasks, "
        "e.g. \"fix A and B\" or \"(1) ... (2) ... (3) ...\"), split at "
        "least one feature per listed deliverable — do NOT collapse them "
        "into one.\n\n"
        'Reply with ONLY a JSON array, e.g.:\n'
        '[{"name": "paginate list", "behavior": "list endpoint returns all pages '
        'without skipping rows", "verification": "pytest tests/test_pagination.py -q", '
        '"depends_on": []}]\n'
        "No prose, no code fences around the array."
    )


def _extract_json_array(text: str) -> str | None:
    """Pull the first balanced JSON array out of the text."""
    if not text or not text.strip():
        return None
    for candidate in _FENCE_RE.findall(text):
        candidate = candidate.strip()
        if candidate.startswith("["):
            return candidate
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_plan(raw: str) -> list[FeaturePlan] | None:
    """Parse planner output into validated FeaturePlans (never raises).

    Returns None when the output is not a usable feature array (caller falls
    back to a single whole-goal feature).
    """
    block = _extract_json_array(_strip_agent_header(raw or ""))
    if block is None:
        return None
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    if len(data) > MAX_FEATURES:
        data = data[:MAX_FEATURES]

    plans: list[FeaturePlan] = []
    names: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            return None
        name = str(entry.get("name") or "").strip()
        behavior = str(entry.get("behavior") or "").strip()
        if not name or not behavior:
            return None
        if name in names:
            return None
        names.add(name)
        verification = str(entry.get("verification") or "").strip()
        if verification:
            decision = check_verification_command(verification)
            if not decision.allowed:
                verification = ""  # policy-rejected -> fall back to full verify
        deps = entry.get("depends_on") or []
        dep_names = tuple(
            str(dep).strip() for dep in deps if isinstance(dep, str) and str(dep).strip()
        )
        # A dependency must reference a known earlier feature (plan order = DAG).
        for dep in dep_names:
            if dep not in names:
                return None
        plans.append(
            FeaturePlan(
                name=name[:80],
                behavior=behavior[:MAX_BEHAVIOR_CHARS],
                verification=verification,
                depends_on=dep_names,
            )
        )
    return plans


def plan_features(
    target: str,
    full_verification: str,
    workspace: Path | None = None,
    *,
    planner_runner=None,
) -> list[FeaturePlan]:
    """Decompose the goal via the read-only planner subagent.

    Falls back to a single whole-goal feature on any planner failure — the
    goal must never die because planning did.
    """
    from harness.agents.runner import run_agent_task as _default_runner

    root = (workspace or get_workdir()).resolve()
    runner = planner_runner or _default_runner
    raw = runner(
        description="decompose goal into verifiable features",
        prompt=build_plan_prompt(target, full_verification),
        agent_type=PLANNER_AGENT,
        cwd=str(root),
    )
    plans = parse_plan(raw)
    if plans:
        return plans
    return [
        FeaturePlan(
            name=(target.strip()[:40] or "goal"),
            behavior=target,
            verification="",
            depends_on=(),
        )
    ]
