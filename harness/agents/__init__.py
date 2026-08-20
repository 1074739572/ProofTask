"""Typed subagents with per-role model binding."""

from harness.agents.registry import (
    agent_model_matrix,
    agent_descriptions,
    get_agent_profile,
    inspect_agent_model,
    lead_model_hint,
    list_agent_types,
    validate_agent_model,
    validate_agent_models,
)
from harness.agents.runner import run_agent_task, spawn_subagent
from harness.agents.schema import build_task_tool_schema

__all__ = [
    "agent_descriptions",
    "agent_model_matrix",
    "build_task_tool_schema",
    "get_agent_profile",
    "inspect_agent_model",
    "lead_model_hint",
    "list_agent_types",
    "run_agent_task",
    "spawn_subagent",
    "validate_agent_model",
    "validate_agent_models",
]
