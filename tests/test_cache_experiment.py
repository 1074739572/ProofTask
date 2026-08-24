"""Keep the cache experiment aligned with the live request assembly shape."""

from __future__ import annotations

from harness.prompts.cache_experiment import simulate_tool_loop


def test_runtime_tail_outperforms_dynamic_system_when_time_changes() -> None:
    legacy = simulate_tool_loop("legacy_system", rounds=5)
    current = simulate_tool_loop("current", rounds=5)
    assert current.hit_rate > legacy.hit_rate
