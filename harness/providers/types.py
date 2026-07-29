"""Anthropic-compatible response shapes shared across providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class ToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class MessageResponse:
    content: list
    stop_reason: str
    model: str | None = None
    usage: Any = None  # Provider-specific usage object
