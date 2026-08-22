"""Parallel, read-only supervision for a running Goal.

The sidecar observes bounded snapshots while the normal Goal state machine
continues.  Its output is advisory; deterministic runner code is the only
component allowed to mutate Task scope or Goal state.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.agents.runner import AgentTaskStats, run_agent_task


SUPERVISOR_AGENT = "goal_supervisor"
SUPERVISOR_ACTIONS = frozenset({
    "continue",
    "watch",
    "redirect",
    "expand_scope",
    "replan",
    "retry",
    "pause_user",
})


@dataclass(frozen=True)
class SupervisorDecision:
    action: str
    summary: str
    reason: str = ""
    next_step: str = ""
    scope_paths: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    confidence: str = "medium"
    unavailable: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "summary": self.summary,
            "reason": self.reason,
            "next_step": self.next_step,
            "scope_paths": list(self.scope_paths),
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "unavailable": self.unavailable,
            "error": self.error,
        }


@dataclass(frozen=True)
class SupervisorRun:
    observation_id: str
    revision: int
    decision: SupervisorDecision
    llm_rounds: int = 0
    stop_reason: str = ""


def _json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_supervisor_decision(raw: str) -> SupervisorDecision:
    data = _json_object(raw)
    if data is None:
        return SupervisorDecision(
            "watch",
            "Supervisor returned no usable decision.",
            unavailable=True,
            error="goal supervisor returned no JSON object",
        )
    action = str(data.get("action") or "").strip()
    summary = str(data.get("summary") or "").strip()[:1_500]
    if action not in SUPERVISOR_ACTIONS:
        return SupervisorDecision(
            "watch",
            summary or "Supervisor returned an unsupported action.",
            unavailable=True,
            error=f"unsupported goal supervisor action: {action or '(empty)'}",
        )
    if not summary:
        return SupervisorDecision(
            "watch",
            "Supervisor omitted its summary.",
            unavailable=True,
            error="goal supervisor decision needs a summary",
        )
    paths = data.get("scope_paths") if isinstance(data.get("scope_paths"), list) else []
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    confidence = str(data.get("confidence") or "medium").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    return SupervisorDecision(
        action=action,
        summary=summary,
        reason=str(data.get("reason") or "").strip()[:2_000],
        next_step=str(data.get("next_step") or "").strip()[:1_500],
        scope_paths=tuple(dict.fromkeys(str(item).strip().replace("\\", "/") for item in paths if str(item).strip())),
        evidence=tuple(str(item).strip()[:500] for item in evidence if str(item).strip()),
        confidence=confidence,
    )


def build_supervisor_prompt(observation: dict[str, Any]) -> str:
    return (
        "You are the read-only global supervisor for one coding Goal. Other agents continue independently while "
        "you inspect this bounded event snapshot. Never edit files, call tools, grant yourself authority, weaken "
        "tests, or change the frozen Goal contract. Return ONLY one JSON object with keys action, summary, reason, "
        "next_step, scope_paths, evidence, confidence.\n\n"
        "Actions:\n"
        "- continue: evidence shows the current direction is healthy.\n"
        "- watch: note a risk but do not interrupt work.\n"
        "- redirect: the current approach is making no useful progress; give a concrete corrected direction.\n"
        "- expand_scope: a boundary request needs exact additional paths that remain inside the Goal contract.\n"
        "- replan: Task ownership or dependencies are wrong but the Goal contract remains valid.\n"
        "- retry: a transient or format failure should retry from its durable checkpoint.\n"
        "- pause_user: genuinely new authority is required, such as external paths, secrets, destructive actions, "
        "deployment, cost, or changing the Goal contract.\n\n"
        "For expand_scope, include only exact requested paths supported by the supplied Goal scope envelope. "
        "Do not approve arbitrary shell commands. Explain the failure and the next action in the Goal language.\n\n"
        f"Observation:\n{json.dumps(observation, ensure_ascii=False, sort_keys=True)[:24_000]}"
    )


def analyze_goal_observation(
    observation: dict[str, Any],
    *,
    cwd: str,
    deadline: float | None,
    cancel_check: Callable[[], bool] | None = None,
    runner=None,
) -> SupervisorRun:
    stats = AgentTaskStats()
    invoke = runner or run_agent_task
    observation_id = str(observation.get("observation_id") or "")
    revision = int(observation.get("revision") or 0)
    prompt = build_supervisor_prompt(observation)
    try:
        raw = invoke(
            description=f"supervise Goal event {observation.get('event') or 'update'}",
            prompt=prompt,
            agent_type=SUPERVISOR_AGENT,
            cwd=cwd,
            max_rounds=1,
            cancel_check=cancel_check,
            deadline=deadline,
            stats=stats,
            tools_override=(),
        )
    except Exception as exc:
        decision = SupervisorDecision(
            "watch",
            "Global supervisor could not analyze this event.",
            unavailable=True,
            error=f"goal supervisor failed: {type(exc).__name__}: {exc}",
        )
    else:
        if stats.stop_reason in {"provider_error", "configuration_error", "deadline", "cancelled"}:
            decision = SupervisorDecision(
                "watch",
                "Global supervisor was unavailable; deterministic Goal rules remain active.",
                unavailable=True,
                error=f"goal supervisor stopped: {stats.stop_reason}",
            )
        else:
            decision = parse_supervisor_decision(raw)
            if decision.unavailable:
                retry_stats = AgentTaskStats()
                try:
                    corrected = invoke(
                        description=f"repair Goal supervision decision {observation.get('event') or 'update'}",
                        prompt=(
                            prompt
                            + "\n\nYour previous response did not match the required JSON contract. "
                            "Return one corrected JSON object only. Previous response:\n"
                            + str(raw)[-2_000:]
                        ),
                        agent_type=SUPERVISOR_AGENT,
                        cwd=cwd,
                        max_rounds=1,
                        cancel_check=cancel_check,
                        deadline=deadline,
                        stats=retry_stats,
                        tools_override=(),
                    )
                except Exception as exc:
                    decision = SupervisorDecision(
                        "watch",
                        "Global supervisor could not repair its invalid decision.",
                        unavailable=True,
                        error=f"goal supervisor correction failed: {type(exc).__name__}: {exc}",
                    )
                else:
                    if retry_stats.stop_reason in {
                        "provider_error",
                        "configuration_error",
                        "deadline",
                        "cancelled",
                    }:
                        decision = SupervisorDecision(
                            "watch",
                            "Global supervisor was unavailable while repairing its decision.",
                            unavailable=True,
                            error=f"goal supervisor correction stopped: {retry_stats.stop_reason}",
                        )
                    else:
                        decision = parse_supervisor_decision(corrected)
                stats.llm_rounds += retry_stats.llm_rounds
                if retry_stats.stop_reason != "completed":
                    stats.stop_reason = retry_stats.stop_reason
    return SupervisorRun(observation_id, revision, decision, stats.llm_rounds, stats.stop_reason)


class ParallelGoalSupervisor:
    """Coalesce observations onto one model lane without blocking normal work."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        operation_timeout_seconds: int,
        cancel_check: Callable[[], bool] | None = None,
        analyzer=analyze_goal_observation,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.timeout_seconds = max(1, min(int(operation_timeout_seconds), 180))
        self._cancel_check = cancel_check
        self._analyzer = analyzer
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="goal-supervisor")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._future: Future[SupervisorRun] | None = None
        self._future_observation_id = ""
        self._pending: dict[str, Any] | None = None
        self._ready: list[SupervisorRun] = []
        self._closed = False

    def _cancelled(self) -> bool:
        return self._stop.is_set() or bool(self._cancel_check and self._cancel_check())

    def _run(self, observation: dict[str, Any]) -> SupervisorRun:
        return self._analyzer(
            observation,
            cwd=self.cwd,
            deadline=time.monotonic() + self.timeout_seconds,
            cancel_check=self._cancelled,
        )

    def _submit_locked(self, observation: dict[str, Any]) -> None:
        self._future_observation_id = str(observation.get("observation_id") or "")
        self._future = self._executor.submit(self._run, dict(observation))

    def observe(self, observation: dict[str, Any]) -> str:
        item = dict(observation)
        item.setdefault("observation_id", f"obs_{uuid.uuid4().hex[:12]}")
        with self._lock:
            if self._closed:
                return str(item["observation_id"])
            if self._future is None:
                self._submit_locked(item)
            else:
                # The latest snapshot subsumes older unstarted snapshots.
                self._pending = item
        return str(item["observation_id"])

    def _take_finished_locked(self) -> SupervisorRun | None:
        future = self._future
        if future is None or not future.done():
            return None
        self._future = None
        self._future_observation_id = ""
        try:
            result = future.result()
        except Exception as exc:
            result = SupervisorRun(
                "",
                0,
                SupervisorDecision(
                    "watch",
                    "Global supervisor crashed while analyzing an event.",
                    unavailable=True,
                    error=f"goal supervisor future failed: {type(exc).__name__}: {exc}",
                ),
            )
        pending = self._pending
        self._pending = None
        if pending is not None and not self._closed:
            self._submit_locked(pending)
        return result

    def poll(self) -> list[SupervisorRun]:
        with self._lock:
            results = list(self._ready)
            self._ready.clear()
            result = self._take_finished_locked()
            if result is not None:
                results.append(result)
            return results

    def review(self, observation: dict[str, Any]) -> SupervisorRun:
        # Permission and terminal-failure boundaries decide the next state.
        # They must not wait behind a long-running advisory observation: doing
        # so made the decision time out at the exact point it was needed.
        item = dict(observation)
        item.setdefault("observation_id", f"obs_{uuid.uuid4().hex[:12]}")
        return self._run(item)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
