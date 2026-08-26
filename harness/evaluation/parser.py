"""Findings parsing for the independent evaluator (L5).

The evaluator model returns a JSON object; this module extracts and validates
it from the raw text (the model may wrap it in prose / code fences). Parsing
is a pure function so it is fully unit-testable without any LLM call.

Expected JSON shape::

    {
      "passed": true | false,
      "summary": "一句话结论",
      "findings": [
        {"issue": "...", "severity": "high|medium|low", "evidence": "..."}
      ]
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("high", "medium", "low")
ROUTES = ("pass", "implementation_fix", "test_gap", "replan", "blocked")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class Findings:
    passed: bool | None  # None = could not parse / malformed
    summary: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    route: str = "blocked"
    affected_task_ids: list[str] = field(default_factory=list)
    # A replan is valid only when the evaluator names the concrete production
    # paths that fall outside the frozen Task contract.
    required_write_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.passed is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "findings": self.items,
            "error": self.error,
            "route": self.route,
            "affected_task_ids": list(self.affected_task_ids),
            "required_write_paths": list(self.required_write_paths),
        }


def _extract_json_block(text: str) -> str | None:
    """Pull the first balanced JSON object out of the text."""
    if not text or not text.strip():
        return None
    for candidate in _FENCE_RE.findall(text):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            return candidate
    start = text.find("{")
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
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _required_write_paths(value: Any) -> list[str]:
    """Normalize explicit evaluator scope requests without reading prose."""
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        path = str(item or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if (
            not path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:/", path)
            or ".." in path.split("/")
            or path in paths
        ):
            continue
        paths.append(path[:300])
        if len(paths) == 16:
            break
    return paths


def parse_findings(raw: str) -> Findings:
    """Parse the evaluator's raw text into structured Findings (never raises)."""
    block = _extract_json_block(raw or "")
    if block is None:
        return Findings(passed=None, error="no JSON object found in evaluator output")

    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        return Findings(passed=None, error=f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return Findings(passed=None, error="evaluator output is not a JSON object")

    passed = data.get("passed")
    if not isinstance(passed, bool):
        return Findings(passed=None, error="missing/ invalid 'passed' (must be true/false)")

    summary = str(data.get("summary") or "")
    items_raw = data.get("findings")
    items: list[dict[str, Any]] = []
    if isinstance(items_raw, list):
        for entry in items_raw:
            if not isinstance(entry, dict):
                continue
            items.append(
                {
                    "issue": str(entry.get("issue") or ""),
                    "severity": (
                        entry.get("severity")
                        if entry.get("severity") in SEVERITIES
                        else "medium"
                    ),
                    "evidence": str(entry.get("evidence") or ""),
                }
            )
    route = str(data.get("route") or ("pass" if passed else "implementation_fix"))
    if route not in ROUTES:
        route = "blocked"
    if passed and (route != "pass" or any(item["severity"] == "high" for item in items)):
        return Findings(
            passed=None,
            error="inconsistent evaluator verdict: a passing result requires route='pass' and no high findings",
        )
    if not passed and route == "pass":
        return Findings(passed=None, error="inconsistent evaluator verdict: failed result cannot use route='pass'")
    raw_affected = data.get("affected_task_ids")
    affected = [str(value)[:80] for value in raw_affected if str(value).strip()][:8] if isinstance(raw_affected, list) else []
    return Findings(
        passed=passed,
        summary=summary,
        items=items,
        route=route,
        affected_task_ids=affected,
        required_write_paths=_required_write_paths(data.get("required_write_paths")),
    )
