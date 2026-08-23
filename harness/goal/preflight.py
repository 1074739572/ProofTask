"""Static provider/model preflight for the durable Goal pipeline.

The preflight deliberately does not spend tokens or make a network request.
It catches the failures that are deterministic locally (wrong model/provider,
unsupported reasoning effort, and missing key) before a Goal starts a wave of
jobs.  Connectivity remains an execution-time concern and is reported with
the provider error returned by the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from harness.agents.registry import AgentModelBinding, agent_model_matrix


DISCOVERY_AGENT_TYPES = tuple(
    f"goal_discovery_{role}"
    for role in ("requirement", "architecture", "implementation", "tests", "history")
)

DRAFT_AGENT_TYPES = ("goal_intake", "goal_planner", "goal_plan_reviewer", *DISCOVERY_AGENT_TYPES)
EXECUTION_AGENT_TYPES = (
    "goal_test_writer",
    "goal_worker",
    "evaluator",
    "goal_repair_planner",
    "goal_test_impact",
)


@dataclass(frozen=True)
class GoalPreflight:
    """Complete static route report for one Goal lifecycle boundary."""

    bindings: tuple[AgentModelBinding, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(binding.error for binding in self.bindings if binding.error)

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self, *, title: str = "Goal provider preflight") -> str:
        lines = [f"{title}: {'ok' if self.ok else 'failed'}"]
        for binding in self.bindings:
            route = f"{binding.model_id} via {binding.provider_id or '?'}"
            effort = binding.reasoning_effort or "model default"
            key = "key ok" if binding.api_key_configured else f"key missing ({binding.api_key_env or '?'})"
            suffix = f"; {binding.error}" if binding.error else ""
            lines.append(f"  {binding.agent_type}: {route}; effort={effort}; {key}{suffix}")
        return "\n".join(lines)


def preflight_goal_agents(agent_types: Iterable[str]) -> GoalPreflight:
    """Return all static route problems in stable role order."""

    return GoalPreflight(tuple(agent_model_matrix(agent_types)))


def format_goal_preflight(agent_types: Iterable[str]) -> str:
    return preflight_goal_agents(agent_types).format()
