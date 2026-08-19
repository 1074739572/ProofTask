"""Verification adapter contracts used by Goal planning and execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True)
class VerificationContext:
    workspace: Path
    command: str = ""
    test_roots: tuple[str, ...] = ()


class VerificationAdapter(Protocol):
    id: str

    def discover(self, context: VerificationContext): ...
    def normalize_selector(self, value: str) -> str: ...
    def build_command(self, selectors: Sequence[str]) -> str: ...
    def run(self, command: str, context: VerificationContext, *, timeout_s: float | None = None): ...
