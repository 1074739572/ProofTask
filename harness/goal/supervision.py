"""Evidence-based supervision for bounded Goal subagent slices.

The model may use tools and suggest a next step, but this module owns the
decision to continue.  A slice is extended only when its caller supplies
observable progress; model prose and tool volume alone are never enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from harness.agents.runner import AgentTaskStats


@dataclass(frozen=True)
class StagePolicy:
    name: str
    slice_rounds: int
    max_idle_slices: int


@dataclass(frozen=True)
class StageProgress:
    advanced: bool
    summary: str
    checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageSlice:
    number: int
    stats: AgentTaskStats
    progress: StageProgress
    idle_slices: int


@dataclass(frozen=True)
class SupervisedRun:
    raw: str
    stats: AgentTaskStats
    slices: int
    idle_slices: int
    stalled: bool
    checkpoint: dict[str, Any]


def emit_stage_supervision(event: str, **payload: Any) -> None:
    from harness.ui import events

    if events.is_enabled():
        events.emit("goal_stage_supervision", event=event, **payload)


class StageSupervisor:
    """Run tool-using work in restartable slices with evidence checkpoints."""

    def __init__(self, policy: StagePolicy):
        self.policy = policy

    def run(
        self,
        *,
        invoke: Callable[[str, str, int, AgentTaskStats], str],
        initial_prompt: str,
        initial_description: str,
        continuation_prompt: Callable[[int, StageProgress, int], str],
        continuation_description: Callable[[int], str],
        snapshot: Callable[[], Any],
        assess_progress: Callable[[Any, Any, AgentTaskStats], StageProgress],
        on_slice: Callable[[StageSlice], None] | None = None,
    ) -> SupervisedRun:
        """Run until a final response, interruption, provider error, or stall.

        ``max_rounds`` means the agent was still using tools when its current
        slice ended.  It is not a failure and is the only condition that can
        cause an automatic continuation.
        """

        before = snapshot()
        prompt = initial_prompt
        description = initial_description
        slice_number = 1
        idle_slices = 0
        checkpoint: dict[str, Any] = {}

        while True:
            stats = AgentTaskStats()
            emit_stage_supervision(
                "slice_started",
                stage=self.policy.name,
                slice=slice_number,
                round_limit=self.policy.slice_rounds,
            )
            raw = invoke(prompt, description, slice_number, stats)
            after = snapshot()
            progress = assess_progress(before, after, stats)
            checkpoint = dict(progress.checkpoint)
            idle_slices = 0 if progress.advanced else idle_slices + 1
            current = StageSlice(slice_number, stats, progress, idle_slices)
            if on_slice is not None:
                on_slice(current)
            emit_stage_supervision(
                "slice_finished",
                stage=self.policy.name,
                slice=slice_number,
                stop_reason=stats.stop_reason,
                rounds=stats.llm_rounds,
                tools=stats.tool_count,
                progress=progress.advanced,
                progress_summary=progress.summary,
                idle_slices=idle_slices,
                checkpoint=checkpoint,
            )
            if stats.stop_reason != "max_rounds":
                return SupervisedRun(raw, stats, slice_number, idle_slices, False, checkpoint)
            if idle_slices >= self.policy.max_idle_slices:
                emit_stage_supervision(
                    "stalled",
                    stage=self.policy.name,
                    slice=slice_number,
                    idle_slices=idle_slices,
                    reason=progress.summary,
                    checkpoint=checkpoint,
                )
                return SupervisedRun(raw, stats, slice_number, idle_slices, True, checkpoint)

            next_slice = slice_number + 1
            emit_stage_supervision(
                "continued" if progress.advanced else "redirected",
                stage=self.policy.name,
                slice=next_slice,
                reason=progress.summary,
                checkpoint=checkpoint,
            )
            prompt = continuation_prompt(next_slice, progress, idle_slices)
            description = continuation_description(next_slice)
            before = after
            slice_number = next_slice
