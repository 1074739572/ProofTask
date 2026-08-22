"""Terminal reports for local usage ledger (Claude/Gemini/OpenCode-style)."""

from __future__ import annotations

import re
import sys
from datetime import date

from harness.usage.store import (
    UsageTotals,
    daily_totals,
    date_range,
    month_days,
    totals_for_day,
    totals_for_days,
)

# Windows GBK consoles often cannot print ░█▁… — use ASCII there.
_WIN_SAFE = sys.platform == "win32"
_SPARK = ".:-=+*#%@" if _WIN_SAFE else "▁▂▃▄▅▆▇█"
_FILL = "#" if _WIN_SAFE else "█"
_EMPTY = "." if _WIN_SAFE else "░"


def format_tokens(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        value = n / 1_000_000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}M"
    if n >= 1_000:
        value = n / 1_000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(n)


def bar(
    ratio: float,
    *,
    width: int = 20,
    fill: str | None = None,
    empty: str | None = None,
) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    fill_ch = fill if fill is not None else _FILL
    empty_ch = empty if empty is not None else _EMPTY
    return fill_ch * filled + empty_ch * (width - filled)


def sparkline(values: list[int]) -> str:
    if not values:
        return ""
    peak = max(values) or 1
    chars: list[str] = []
    last = len(_SPARK) - 1
    for value in values:
        index = int(round((value / peak) * last))
        chars.append(_SPARK[index])
    return "".join(chars)


def _pct(rate: float) -> str:
    return f"{100 * rate:.0f}%"


def _model_rows(totals: UsageTotals, *, limit: int = 8) -> list[str]:
    if not totals.by_model:
        return ["  (no model breakdown)"]
    ranked = sorted(
        totals.by_model.items(),
        key=lambda item: item[1]["hit"] + item[1]["miss"],
        reverse=True,
    )[:limit]
    peak = max((row["hit"] + row["miss"] for _, row in ranked), default=1) or 1
    lines: list[str] = []
    for model, row in ranked:
        inp = row["hit"] + row["miss"]
        rate = row["hit"] / inp if inp else 0.0
        output = "?" if row.get("unknown_output_calls", 0) else format_tokens(row["out"])
        lines.append(
            f"  {model:<22} {bar(inp / peak, width=12)}  "
            f"in {format_tokens(inp):>6}  hit {_pct(rate):>3}  "
            f"out {output:>5}  x{row['calls']}"
            + (f" ({row['unknown_output_calls']} unknown)" if row.get("unknown_output_calls", 0) else "")
        )
    return lines


def _summary_block(title: str, totals: UsageTotals) -> str:
    if totals.calls == 0:
        return f"{title}\n  (no usage recorded)"
    hit_bar = bar(totals.hit_rate, width=22)
    lines = [
        title,
        f"  Input    {format_tokens(totals.input_tokens):>8} tok",
        f"    hit    {hit_bar}  {format_tokens(totals.hit):>8}  {_pct(totals.hit_rate)}",
        f"    miss   {bar(1 - totals.hit_rate if totals.input_tokens else 0, width=22)}  "
        f"{format_tokens(totals.miss):>8}  {_pct(1 - totals.hit_rate if totals.input_tokens else 0)}",
        f"  Output   {format_tokens(totals.out):>8} tok"
        + (f" ({totals.unknown_output_calls} calls unknown)" if totals.unknown_output_calls else ""),
        f"  Calls    {totals.calls:>8}",
        "",
        "  By model",
        *_model_rows(totals),
    ]
    return "\n".join(lines)


def format_day_report(day: date | None = None) -> str:
    day = day or date.today()
    totals = totals_for_day(day)
    return _summary_block(f"Usage - {day.isoformat()} (day)", totals)


def format_week_report(end: date | None = None) -> str:
    end = end or date.today()
    days = date_range(end, 7)
    series = daily_totals(days)
    totals = totals_for_days(days)
    peak = max((item.input_tokens for _, item in series), default=1) or 1
    lines = [
        f"Usage - last 7 days ending {end.isoformat()}",
        "",
        "  Input tokens",
    ]
    for day, item in series:
        mark = " <- today" if day == end else ""
        ratio = item.input_tokens / peak if peak else 0
        lines.append(
            f"  {day.strftime('%m/%d')}  {bar(ratio, width=14)}  "
            f"{format_tokens(item.input_tokens):>6}  hit {_pct(item.hit_rate)}{mark}"
        )
    lines.extend(
        [
            "",
            f"  Spark in  {sparkline([item.input_tokens for _, item in series])}",
            f"  Spark hit {sparkline([int(round(100 * item.hit_rate)) for _, item in series])}",
            "",
            _summary_block("Totals (7 days)", totals),
        ]
    )
    return "\n".join(lines)


def format_month_report(year: int, month: int) -> str:
    days = month_days(year, month)
    series: list[tuple[date, UsageTotals]] = []
    all_totals = UsageTotals()
    daily_inputs: list[int] = []
    for day in days:
        day_totals = totals_for_day(day)
        daily_inputs.append(day_totals.input_tokens)
        if day_totals.calls:
            series.append((day, day_totals))
            for model, row in day_totals.by_model.items():
                bucket = all_totals.by_model.setdefault(
                    model, {"hit": 0, "miss": 0, "out": 0, "calls": 0, "unknown_output_calls": 0}
                )
                bucket["hit"] += row["hit"]
                bucket["miss"] += row["miss"]
                bucket["out"] += row["out"]
                bucket["calls"] += row["calls"]
                bucket["unknown_output_calls"] += row.get("unknown_output_calls", 0)
            all_totals.hit += day_totals.hit
            all_totals.miss += day_totals.miss
            all_totals.out += day_totals.out
            all_totals.calls += day_totals.calls
            all_totals.unknown_output_calls += day_totals.unknown_output_calls

    title = f"Usage - {year:04d}-{month:02d} (month)"
    if all_totals.calls == 0:
        return f"{title}\n  (no usage recorded)"

    lines = [
        title,
        f"  Days with usage: {len(series)} / {len(days)}",
        f"  Spark in  {sparkline(daily_inputs)}",
        "",
        _summary_block("Totals", all_totals),
    ]
    if series:
        peak = max(item.input_tokens for _, item in series) or 1
        lines.append("")
        lines.append("  Busiest days")
        top = sorted(series, key=lambda pair: pair[1].input_tokens, reverse=True)[:7]
        for day, item in top:
            lines.append(
                f"  {day.isoformat()}  {bar(item.input_tokens / peak, width=12)}  "
                f"in {format_tokens(item.input_tokens):>6}  hit {_pct(item.hit_rate)}"
            )
    return "\n".join(lines)


def format_year_report(year: int) -> str:
    months: list[tuple[int, UsageTotals]] = []
    grand = UsageTotals()
    for month in range(1, 13):
        days = month_days(year, month)
        month_totals = totals_for_days(days)
        if month_totals.calls:
            months.append((month, month_totals))
        grand.hit += month_totals.hit
        grand.miss += month_totals.miss
        grand.out += month_totals.out
        grand.calls += month_totals.calls
        grand.unknown_output_calls += month_totals.unknown_output_calls
        for model, row in month_totals.by_model.items():
            bucket = grand.by_model.setdefault(
                model, {"hit": 0, "miss": 0, "out": 0, "calls": 0, "unknown_output_calls": 0}
            )
            bucket["hit"] += row["hit"]
            bucket["miss"] += row["miss"]
            bucket["out"] += row["out"]
            bucket["calls"] += row["calls"]
            bucket["unknown_output_calls"] += row.get("unknown_output_calls", 0)

    title = f"Usage - {year} (year)"
    if grand.calls == 0:
        return f"{title}\n  (no usage recorded)"

    peak = max((item.input_tokens for _, item in months), default=1) or 1
    lines = [title, "", "  By month"]
    for month, item in months:
        lines.append(
            f"  {year}-{month:02d}  {bar(item.input_tokens / peak, width=14)}  "
            f"in {format_tokens(item.input_tokens):>6}  hit {_pct(item.hit_rate)}  "
            f"x{item.calls}"
        )
    lines.extend(["", _summary_block("Totals", grand)])
    return "\n".join(lines)


def format_usage_welcome_line(day: date | None = None) -> str | None:
    totals = totals_for_day(day or date.today())
    if totals.calls == 0:
        return None
    return (
        f"today in {format_tokens(totals.input_tokens)} | "
        f"out {format_tokens(totals.out)} | "
        f"hit {_pct(totals.hit_rate)} | "
        f"{totals.calls} calls"
    )


def parse_usage_arg(arg: str) -> tuple[str, date | None, int | None, int | None]:
    """Return (kind, day, year, month) for /usage arguments."""
    text = (arg or "").strip().lower()
    if not text or text in ("today", "day"):
        return "day", date.today(), None, None
    if text in ("week", "7d", "7"):
        return "week", date.today(), None, None
    if text in ("month", "m"):
        today = date.today()
        return "month", None, today.year, today.month
    if text in ("year", "y"):
        return "year", None, date.today().year, None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "day", date.fromisoformat(text), None, None
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year_s, month_s = text.split("-")
        return "month", None, int(year_s), int(month_s)
    if re.fullmatch(r"\d{4}", text):
        return "year", None, int(text), None

    raise ValueError(
        "Usage: /usage [today|week|month|year|YYYY-MM-DD|YYYY-MM|YYYY]"
    )


def format_usage_report(arg: str = "") -> str:
    try:
        kind, day, year, month = parse_usage_arg(arg)
    except ValueError as exc:
        return str(exc)

    if kind == "day":
        assert day is not None
        return format_day_report(day)
    if kind == "week":
        return format_week_report(day or date.today())
    if kind == "month":
        assert year is not None and month is not None
        return format_month_report(year, month)
    assert year is not None
    return format_year_report(year)


def handle_usage_command(query: str) -> str:
    parts = query.strip().split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""
    body = format_usage_report(arg)
    footer = (
        "\n\n"
        "Commands: /usage  /usage week  /usage month  /usage year\n"
        "          /usage 2026-07-11  /usage 2026-07  /usage 2026\n"
        "Data: .project/usage/YYYY-MM-DD.jsonl  (kept across /clear)"
    )
    return body + footer
