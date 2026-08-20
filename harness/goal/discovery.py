"""Deterministic repository map and bounded discovery input preparation."""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
import threading
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from harness.agents.runner import AgentTaskStats, run_agent_task
from harness.goal.discovery_models import DiscoveryJob, DiscoveryManifest, Evidence
from harness.goal.discovery_store import save_job_state, save_manifest, save_report


EXCLUDED_DIRS = frozenset({
    ".git", ".project", ".venv", "venv", "node_modules", "dist", "build",
    "coverage", "__pycache__", ".mypy_cache", ".pytest_cache", ".worktrees",
    ".task_outputs", ".local", ".goal-smoke", ".playwright-mcp", ".rag",
})
EXCLUDED_NAMES = frozenset({".env", ".env.local", ".env.production", "id_rsa"})
TEXT_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".css", ".html"})
MAX_FILE_BYTES = 512_000
DISCOVERY_ROLES = (
    "requirement", "architecture", "implementation", "tests", "history",
)
DISCOVERY_MAX_ROUNDS = 16
DISCOVERY_FILE_LIMIT = 16
DEFAULT_DISCOVERY_CONCURRENCY = 1


@dataclass(frozen=True)
class FileShard:
    path: str
    sha256: str
    start_line: int
    end_line: int
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data["imports"] = list(self.imports)
        return data


def _safe_path(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def iter_readable_files(root: str | Path, paths: Iterable[str] | None = None) -> tuple[Path, ...]:
    base = Path(root).expanduser().resolve()
    candidates = ((base / item).resolve() for item in paths) if paths else base.rglob("*")
    found: list[Path] = []
    for path in candidates:
        if not _safe_path(base, path) or not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name in EXCLUDED_NAMES or path.name.startswith(".env"):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES:
            continue
        found.append(path)
    return tuple(sorted(set(found), key=lambda item: item.relative_to(base).as_posix()))


def _python_outline(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (), ()
    symbols: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(dict.fromkeys(symbols)), tuple(dict.fromkeys(imports))


def build_repo_map(root: str | Path, paths: Iterable[str] | None = None) -> tuple[FileShard, ...]:
    base = Path(root).expanduser().resolve()
    shards: list[FileShard] = []
    for path in iter_readable_files(base, paths):
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        symbols, imports = _python_outline(text) if path.suffix.lower() == ".py" else ((), ())
        lines = max(1, text.count("\n") + 1)
        shards.append(FileShard(
            path=path.relative_to(base).as_posix(),
            sha256=hashlib.sha256(raw).hexdigest(),
            start_line=1,
            end_line=lines,
            symbols=symbols,
            imports=imports,
        ))
    return tuple(shards)


def git_revision(root: str | Path) -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(Path(root).resolve()), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _parse_report(raw: str) -> dict:
    """Extract one complete evidence report from a model wrapper or code fence."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and any(key in data for key in ("evidence", "candidate_scope", "related_tests", "gaps")):
            return data
    return {}


def _target_scope(target: str, names: list[str]) -> tuple[str, ...]:
    """Find repository roots named by a Goal target without trusting arbitrary paths."""
    normalized = target.replace("\\", "/")
    exact = [path for path in names if path in normalized]
    roots: list[str] = []
    for path in exact:
        parts = path.split("/")
        if len(parts) > 1:
            roots.append(parts[0])
    # Targets often contain a relative path without an exact absolute match.
    for candidate in re.findall(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", normalized):
        candidate = candidate.lstrip("./")
        if candidate in names:
            roots.append(candidate.split("/")[0])
    return tuple(dict.fromkeys(root for root in roots if root))


def _assigned_paths(
    role: str,
    shards: tuple[FileShard, ...],
    *,
    target: str = "",
    limit: int = DISCOVERY_FILE_LIMIT,
) -> tuple[str, ...]:
    """Assign a small, target-local evidence set to each discovery role."""
    names = [item.path for item in shards]
    roots = _target_scope(target, names)
    scoped = [path for path in names if not roots or any(path == root or path.startswith(f"{root}/") for root in roots)]
    target_files = [path for path in names if path in target.replace("\\", "/")]
    is_test = lambda path: "/test" in path.lower() or path.startswith("test")

    if role == "tests":
        selected = [*target_files, *(path for path in scoped if is_test(path))]
    elif role == "history":
        selected = [*target_files, *(path for path in scoped if path.startswith("docs/") or "/docs/" in path or "/goal" in path)]
    elif role == "requirement":
        selected = [*target_files, *(path for path in scoped if path.lower().endswith((".md", ".txt")))]
    else:
        selected = [*target_files, *(path for path in scoped if not is_test(path))]
    if not selected:
        selected = scoped or names
    return tuple(dict.fromkeys(selected))[:limit]


def _prompt(target: str, role: str, base_revision: str, shards: tuple[FileShard, ...], paths: tuple[str, ...]) -> str:
    index = {item.path: item for item in shards}
    outline = [index[path].to_dict() for path in paths if path in index]
    return (
        "Collect repository evidence for a verified coding Goal.\n"
        f"Goal: {target}\nRole: {role}\nBase revision: {base_revision}\n"
        f"Assigned files: {json.dumps(outline, ensure_ascii=False)}\n\n"
        f"Read only the files needed for the role (at most {len(paths)}); reserve your final response for the report.\n"
        "Return ONLY JSON: {\"evidence\":[{\"path\":\"assigned file\",\"symbol\":\"optional\","
        "\"lines\":[1,2],\"excerpt\":\"exact text from those lines\",\"claim\":\"fact grounded in that file\"}],"
        "\"candidate_scope\":[\"assigned file\"],\"related_tests\":[\"assigned test file\"],"
        "\"gaps\":[\"missing fact\"]}. Do not invent paths or claims."
    )


class DiscoverySupervisor:
    """Fan out read-only evidence work and merge only validated citations."""

    def __init__(self, *, runner=run_agent_task, concurrency: int = DEFAULT_DISCOVERY_CONCURRENCY, request_interval_s: float = 2.0):
        self.runner = runner
        self.concurrency = max(1, min(int(concurrency), len(DISCOVERY_ROLES)))
        self.request_interval_s = max(0.0, float(request_interval_s))
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0

    def run(
        self,
        *,
        goal_id: str,
        target: str,
        workspace: str | Path,
        deadline: float | None = None,
        cancel_check=None,
        roles: tuple[str, ...] = DISCOVERY_ROLES,
    ) -> DiscoveryManifest:
        root = Path(workspace).expanduser().resolve()
        shards = build_repo_map(root)
        revision = git_revision(root)
        jobs = [DiscoveryJob(id=f"{role}-1", role=role, read_paths=_assigned_paths(role, shards, target=target)) for role in roles]
        for job in jobs:
            save_job_state(root, goal_id, job.to_dict())
            _emit_discovery_job(goal_id, job, event="queued")

        completed: list[tuple[DiscoveryJob, dict]] = []
        def execute(job: DiscoveryJob) -> tuple[DiscoveryJob, dict]:
            started = time.time()
            running = DiscoveryJob(**{**job.to_dict(), "status": "running", "started_at": started})
            save_job_state(root, goal_id, running.to_dict())
            _emit_discovery_job(goal_id, running, event="started")
            if cancel_check is not None and cancel_check():
                return running, {"error": "cancelled"}
            stats = AgentTaskStats()
            try:
                # Provider limits are commonly account-wide, so a local
                # thread pool must still serialize requests to the same API.
                with self._request_lock:
                    wait = self.request_interval_s - (time.monotonic() - self._last_request_at)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_request_at = time.monotonic()
                raw = self.runner(
                    description=f"discover {job.role} evidence",
                    prompt=_prompt(target, job.role, revision, shards, job.read_paths),
                    agent_type=f"goal_discovery_{job.role}", cwd=str(root),
                    max_rounds=DISCOVERY_MAX_ROUNDS, tools_override=("read_file",),
                    read_paths=job.read_paths, deadline=deadline, cancel_check=cancel_check, stats=stats,
                )
            except Exception as exc:
                return running, {"error": f"{type(exc).__name__}: {exc}", "failure_kind": "exception"}
            report = _parse_report(raw)
            if not report:
                report = {
                    "error": "discovery response did not contain a valid JSON evidence report",
                    "failure_kind": "invalid_report",
                    "response_tail": raw[-800:],
                }
            if stats.stop_reason in {"configuration_error", "provider_error", "empty_response", "deadline", "cancelled"}:
                report.setdefault("failure_kind", stats.stop_reason)
            error_text = str(report.get("error") or raw).lower()
            if "429" in error_text or "ratelimit" in error_text or "too many requests" in error_text or "max retries" in error_text:
                report["error"] = "provider rate limited discovery job"
                report["retryable"] = True
            return running, report

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="goal-discovery") as pool:
            future_jobs = {pool.submit(execute, job): job for job in jobs}
            for future in concurrent.futures.as_completed(future_jobs):
                try:
                    running, report = future.result()
                except Exception as exc:
                    # A single provider/job failure is evidence for that job,
                    # not a reason to discard successful sibling reports.
                    running = future_jobs[future]
                    report = {"error": f"{type(exc).__name__}: {exc}"}
                error = str(report.get("error") or "") or None
                error_text = (error or "").lower()
                retryable = bool(report.get("retryable")) or any(
                    marker in error_text
                    for marker in ("429", "ratelimit", "too many requests", "max retries", "provider rate limited")
                )
                finished = DiscoveryJob(**{**running.to_dict(), "status": "failed" if error else "done", "error": error, "retryable": retryable, "retry_after": time.time() + 30 if retryable else 0.0, "finished_at": time.time()})
                report_path = save_report(root, goal_id, finished.id, report)
                finished = DiscoveryJob(**{**finished.to_dict(), "report_path": str(report_path.relative_to(root)).replace("\\", "/")})
                save_job_state(root, goal_id, finished.to_dict())
                _emit_discovery_job(goal_id, finished, event="failed" if error else "completed", failure_kind=report.get("failure_kind"))
                completed.append((finished, report))

        by_path = {item.path: item for item in shards}
        evidence: list[Evidence] = []
        gaps: list[str] = []
        for job, report in sorted(completed, key=lambda item: item[0].id):
            if job.error:
                gaps.append(f"{job.role} discovery unavailable: {job.error[:300]}")
            gaps.extend(str(item)[:500] for item in report.get("gaps", []) if str(item).strip())
            for item in report.get("evidence", []) if isinstance(report.get("evidence"), list) else []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").replace("\\", "/")
                shard = by_path.get(path)
                lines = item.get("lines")
                if shard is None or path not in job.read_paths or not isinstance(lines, list) or len(lines) != 2:
                    continue
                try:
                    begin, end = int(lines[0]), int(lines[1])
                except (TypeError, ValueError):
                    continue
                claim = str(item.get("claim") or "").strip()[:1_000]
                excerpt = str(item.get("excerpt") or "")
                if not claim or not excerpt or begin < 1 or end < begin or end > shard.end_line:
                    continue
                try:
                    source_text = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
                    actual_excerpt = "\n".join(source_text[begin - 1:end])
                except OSError:
                    continue
                if actual_excerpt != excerpt or hashlib.sha256(actual_excerpt.encode("utf-8")).hexdigest() != str(item.get("excerpt_sha256") or hashlib.sha256(excerpt.encode("utf-8")).hexdigest()):
                    continue
                evidence.append(Evidence(
                    id=f"E{len(evidence) + 1}", path=path, sha256=shard.sha256, claim=claim,
                    symbol=str(item.get("symbol") or "")[:160], lines=(begin, end),
                    base_revision=revision, source_job=job.id,
                    excerpt_sha256=hashlib.sha256(actual_excerpt.encode("utf-8")).hexdigest(),
                ))
        manifest = DiscoveryManifest(
            goal_id=goal_id, base_revision=revision, repo_files=tuple(item.path for item in shards),
            shards=tuple(item.to_dict() for item in shards), evidence=tuple(evidence),
            jobs=tuple(job for job, _ in sorted(completed, key=lambda item: item[0].id)), gaps=tuple(dict.fromkeys(gaps)),
        )
        save_manifest(root, goal_id, manifest.to_dict())
        _emit_discovery_job(goal_id, None, event="wave_completed", completed=len(completed), total=len(jobs))
        return manifest


def _emit_discovery_job(goal_id: str, job: DiscoveryJob | None, *, event: str, **extra: object) -> None:
    """Emit bounded discovery progress without coupling classic CLI output."""
    try:
        from harness.ui.events import emit, is_enabled

        if not is_enabled():
            return
        payload: dict[str, object] = {"goal_id": goal_id, "event": event}
        if job is not None:
            payload.update({
                "job_id": job.id,
                "role": job.role,
                "status": job.status,
                "read_path_count": len(job.read_paths),
                # Discovery is deliberately read-only. Surface the bounded
                # assignment so the UI can explain what it is inspecting.
                "read_paths": list(job.read_paths[:12]),
                "tools": ["read_file"],
                "error": (job.error or "")[:1_000],
                "retryable": job.retryable,
                "report_path": job.report_path,
            })
        payload.update(extra)
        emit("goal_discovery_job", **payload)
    except Exception:
        return
