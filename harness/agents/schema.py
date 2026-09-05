"""task tool schema for orchestrate mode."""

from __future__ import annotations

from harness.agents.registry import agent_descriptions, list_agent_types


def build_task_tool_schema() -> dict:
    types = list_agent_types()
    return {
        "name": "task",
        "description": (
            "Spawn a focused subagent with its OWN model and isolated context.\n\n"
            "Agent types (model binding from config/agents.json):\n"
            f"{agent_descriptions()}\n\n"
            "Rules:\n"
            "- Pass a complete prompt (goal, paths, constraints); subagent cannot see parent history\n"
            "- code for scripts/edits; write for Chinese prose; explore for read-only search\n"
            "- Returns a report with status (completed/partial/failed/blocked), "
            "summary, findings, changed_paths, verification, confidence, and next_action; "
            "the lead must verify claims before reporting success"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short label (3-6 words) for progress display",
                },
                "prompt": {
                    "type": "string",
                    "description": "Full instructions for the subagent",
                },
                "agent_type": {
                    "type": "string",
                    "enum": types,
                    "description": "Which worker profile to use",
                },
            },
            "required": ["description", "prompt", "agent_type"],
        },
    }
