"""Small, JSON-safe contracts for Goal repository discovery.

Discovery is evidence collection, not an execution unit.  These models keep
the planner input bounded and make every claim traceable to a file snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    id: str
    path: str
    sha256: str
    claim: str
    symbol: str = ""
    lines: tuple[int, int] = (0, 0)
    base_revision: str = ""
    source_job: str = ""
    excerpt_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["lines"] = list(self.lines)
        return data


@dataclass(frozen=True)
class DiscoveryJob:
    id: str
    role: str
    status: str = "pending"
    read_roots: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    report_path: str = ""
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    retryable: bool = False
    retry_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["read_roots"] = list(self.read_roots)
        data["read_paths"] = list(self.read_paths)
        return data


@dataclass(frozen=True)
class DiscoveryManifest:
    goal_id: str
    base_revision: str
    revision: int = 1
    repo_files: tuple[str, ...] = ()
    shards: tuple[dict[str, Any], ...] = ()
    evidence: tuple[Evidence, ...] = ()
    jobs: tuple[DiscoveryJob, ...] = ()
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "base_revision": self.base_revision,
            "revision": self.revision,
            "repo_files": list(self.repo_files),
            "shards": [dict(item) for item in self.shards],
            "evidence": [item.to_dict() for item in self.evidence],
            "jobs": [item.to_dict() for item in self.jobs],
            "gaps": list(self.gaps),
            "conflicts": list(self.conflicts),
        }
