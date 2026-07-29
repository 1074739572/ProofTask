"""Classic CLI display helpers.

Keep classic output readable without turning it into a full-screen app.  Rich is
used when available for correct width handling; plain text fallback avoids ANSI
or box drawing garbage in constrained terminals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from harness.usage.report import format_tokens
from harness.usage.store import daily_totals, date_range, totals_for_days

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover - exercised by fallback tests via monkeypatch
    _RICH = False
    Console = Group = Panel = Table = Text = box = None  # type: ignore[assignment]


def _console() -> Console | None:
    if not _RICH:
        return None
    return Console(highlight=False, legacy_windows=False)


@dataclass(frozen=True)
class AgentProgressRow:
    name: str
    phase: str
    detail: str = ""


def render_stats_dashboard() -> str:
    """Return a classic-safe usage dashboard.

    With Rich we render to ANSI-aware text using Table/Panel.  Without Rich the
    return value is an aligned plain-text block.
    """
    today = date.today()
    periods = [("Today", 1), ("7 Days", 7), ("30 Days", 30)]

    if not _RICH:
        lines = ["Usage Dashboard", ""]
        for label, days in periods:
            totals = totals_for_days(date_range(today, days))
            lines.append(
                f"{label:<8} in {format_tokens(totals.input_tokens):>7}  "
                f"out {format_tokens(totals.out):>7}  "
                f"hit {100 * totals.hit_rate:>3.0f}%  calls {totals.calls}"
            )
        lines.append("")
        lines.extend(_plain_model_rows())
        return "\n".join(lines)

    table = Table(box=box.SIMPLE_HEAVY, expand=False, show_header=True, header_style="bold cyan")
    table.add_column("Period", no_wrap=True)
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Hit", justify="right")
    table.add_column("Calls", justify="right")
    for label, days in periods:
        totals = totals_for_days(date_range(today, days))
        table.add_row(
            label,
            format_tokens(totals.input_tokens),
            format_tokens(totals.out),
            f"{100 * totals.hit_rate:.0f}%",
            str(totals.calls),
        )

    trend = _trend_text()
    model_table = _rich_model_table()
    group = Group(table, Text(trend, style="dim") if trend else Text(""), model_table)
    console = _console()
    assert console is not None
    with console.capture() as cap:
        console.print(Panel(group, title="Usage Dashboard", border_style="cyan", expand=False))
    return cap.get().rstrip()


def _trend_text() -> str:
    daily = daily_totals(date_range(date.today(), 7))
    if not daily:
        return ""
    values = [item.input_tokens for _, item in daily]
    peak = max(values, default=0) or 1
    chars = "▁▂▃▄▅▆▇█"
    spark = "".join(chars[min(len(chars) - 1, int((v / peak) * (len(chars) - 1)))] for v in values)
    return f"7-day input trend  {spark}"


def _rich_model_table():
    table = Table(box=box.MINIMAL, expand=False, show_header=True, header_style="bold")
    table.add_column("Model")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Hit", justify="right")
    totals = totals_for_days(date_range(date.today(), 30))
    if not totals.by_model:
        table.add_row("(no usage recorded)", "", "", "")
        return table
    ranked = sorted(
        totals.by_model.items(),
        key=lambda kv: kv[1]["hit"] + kv[1]["miss"],
        reverse=True,
    )[:8]
    for model, row in ranked:
        inp = row["hit"] + row["miss"]
        hit = f"{100 * row['hit'] / inp:.0f}%" if inp else "0%"
        table.add_row(model[:28], format_tokens(inp), format_tokens(row["out"]), hit)
    return table


def _plain_model_rows() -> list[str]:
    totals = totals_for_days(date_range(date.today(), 30))
    lines = ["By model (30d)"]
    if not totals.by_model:
        return [*lines, "  (no usage recorded)"]
    ranked = sorted(
        totals.by_model.items(),
        key=lambda kv: kv[1]["hit"] + kv[1]["miss"],
        reverse=True,
    )[:8]
    for model, row in ranked:
        inp = row["hit"] + row["miss"]
        hit = f"{100 * row['hit'] / inp:.0f}%" if inp else "0%"
        lines.append(
            f"  {model[:28]:<28} in {format_tokens(inp):>7}  "
            f"out {format_tokens(row['out']):>7}  hit {hit}"
        )
    return lines


def render_status_footer(*, model: str, mode: str, context_rate: float | None = None, cache_hit_rate: float | None = None, mcp_count: int = 0) -> str:
    parts = [f"model {model}"]
    if mode:
        parts.append(f"mode {mode}")
    if context_rate is not None:
        parts.append(f"ctx {100 * max(0.0, min(1.0, context_rate)):.0f}%")
    if cache_hit_rate is not None:
        parts.append(f"cache {100 * max(0.0, min(1.0, cache_hit_rate)):.0f}%")
    if mcp_count:
        parts.append(f"mcp {mcp_count}")
    line = " · ".join(parts)
    return f"\033[2m{line}\033[0m" if _RICH else line


def render_agent_progress(rows: Iterable[AgentProgressRow]) -> str:
    rows = list(rows)
    if not rows:
        return ""
    if not _RICH:
        return "\n".join(f"{r.name}: {r.phase} {r.detail}".rstrip() for r in rows)
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta", expand=False)
    table.add_column("Agent")
    table.add_column("State")
    table.add_column("Detail")
    for row in rows:
        table.add_row(row.name, row.phase, row.detail[:90])
    console = _console()
    assert console is not None
    with console.capture() as cap:
        console.print(Panel(table, title="Agents", border_style="magenta", expand=False))
    return cap.get().rstrip()


def render_failure_summary(*, user_query: str, errors: list[str], attempted_models: list[str] | None = None, teammate_notes: list[str] | None = None) -> str:
    attempted = ", ".join(attempted_models or []) or "(none)"
    lines = [
        "模型调用失败，已本地收口：",
        f"- 用户问题：{user_query[:160] if user_query else '(unknown)'}",
        f"- 已尝试模型：{attempted}",
    ]
    for err in errors[:4]:
        lines.append(f"- 错误：{err.splitlines()[0][:220]}")
    for note in (teammate_notes or [])[:4]:
        lines.append(f"- 子 Agent：{note.splitlines()[0][:220]}")
    lines.append("- 下一步：用 /model 切换到有余额/有权限的模型，或设置 HARNESS_RECOVERY_MODELS 后重试。")
    return "\n".join(lines)
