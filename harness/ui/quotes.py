"""Daily quote queue: Hitokoto fetch, F1 calendar-day consume, auto-refill <5."""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from harness.settings import PROJECT_DIR

HITOKOTO_URL = "https://v1.hitokoto.cn/?encode=json&charset=utf-8"
QUOTES_PATH = PROJECT_DIR / "daily_quotes.json"
MIN_QUEUE = 5
TARGET_QUEUE = 20
FETCH_TIMEOUT_S = 8.0
FETCH_GAP_S = 0.55  # stay under ~2 QPS

# Offline fallback when queue empty / network fails.
FALLBACK_QUOTES: list[dict[str, str]] = [
    {"hitokoto": "代码能跑只是起点，能读才是遗产。", "from": "fallback"},
    {"hitokoto": "先让它工作，再让它漂亮，最后让它快。", "from": "fallback"},
    {"hitokoto": "今天最好的提交，是把昨天的坑填上。", "from": "fallback"},
    {"hitokoto": "少猜一点，多读一行日志。", "from": "fallback"},
    {"hitokoto": "完成比完美更接近上线。", "from": "fallback"},
    {"hitokoto": "工具是仆人，目标才是主人。", "from": "fallback"},
    {"hitokoto": "写清楚意图，比写花哨实现更值钱。", "from": "fallback"},
]

_lock = threading.Lock()
_refill_started = False


def quotes_path() -> Path:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    return QUOTES_PATH


def _empty_store() -> dict[str, Any]:
    return {"queue": [], "today": None}


def load_store() -> dict[str, Any]:
    path = quotes_path()
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    queue = data.get("queue")
    if not isinstance(queue, list):
        queue = []
    today = data.get("today")
    if today is not None and not isinstance(today, dict):
        today = None
    return {"queue": queue, "today": today}


def save_store(store: dict[str, Any]) -> None:
    path = quotes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {
        "queue": list(store.get("queue") or []),
        "today": store.get("today"),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_item(raw: dict[str, Any]) -> dict[str, str] | None:
    text = str(raw.get("hitokoto") or raw.get("text") or "").strip()
    if not text:
        return None
    return {
        "hitokoto": text,
        "from": str(raw.get("from") or raw.get("from_who") or "").strip(),
        "uuid": str(raw.get("uuid") or "").strip(),
    }


def fetch_hitokoto() -> dict[str, str] | None:
    """One sentence from Hitokoto public API."""
    req = urllib.request.Request(
        HITOKOTO_URL,
        headers={"User-Agent": "improved_harness/daily-quote"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if not isinstance(data, dict):
            return None
        return _normalize_item(data)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def refill_queue(
    *,
    target: int = TARGET_QUEUE,
    min_keep: int = MIN_QUEUE,
    store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch until queue length >= target (or give up after enough tries)."""
    with _lock:
        store = store if store is not None else load_store()
        queue: list = list(store.get("queue") or [])
        seen = {
            (str(item.get("uuid") or ""), str(item.get("hitokoto") or ""))
            for item in queue
            if isinstance(item, dict)
        }
        tries = 0
        max_tries = max(target * 3, 30)
        while len(queue) < target and tries < max_tries:
            tries += 1
            item = fetch_hitokoto()
            if item is None:
                time.sleep(FETCH_GAP_S)
                continue
            key = (item.get("uuid") or "", item["hitokoto"])
            if key in seen:
                time.sleep(FETCH_GAP_S)
                continue
            seen.add(key)
            queue.append(item)
            time.sleep(FETCH_GAP_S)
        store["queue"] = queue
        save_store(store)
        return {
            "ok": len(queue) >= min_keep,
            "queue_len": len(queue),
            "fetched_attempts": tries,
            "path": str(quotes_path()),
        }


def _pick_fallback() -> dict[str, str]:
    item = random.choice(FALLBACK_QUOTES)
    return {"hitokoto": item["hitokoto"], "from": item.get("from", "fallback"), "uuid": ""}


def _format_quote(item: dict[str, str]) -> str:
    text = item.get("hitokoto") or ""
    source = (item.get("from") or "").strip()
    if source and source != "fallback":
        return f"{text}  —— {source}"
    return text


def maybe_refill_async(*, target: int = TARGET_QUEUE) -> None:
    """Background refill when queue is low (non-blocking)."""
    global _refill_started
    with _lock:
        store = load_store()
        qlen = len(store.get("queue") or [])
        if qlen >= MIN_QUEUE:
            return
        if _refill_started:
            return
        _refill_started = True

    def _run() -> None:
        global _refill_started
        try:
            refill_queue(target=target)
        finally:
            with _lock:
                _refill_started = False

    threading.Thread(target=_run, name="quote-refill", daemon=True).start()


def get_daily_quote_item(*, day: date | None = None) -> dict[str, str]:
    """F1: same calendar day → same item; new day pops one from queue."""
    day = day or date.today()
    day_key = day.isoformat()

    with _lock:
        store = load_store()
        today = store.get("today")
        if isinstance(today, dict) and today.get("date") == day_key and today.get("hitokoto"):
            item = {
                "hitokoto": str(today["hitokoto"]),
                "from": str(today.get("from") or ""),
                "uuid": str(today.get("uuid") or ""),
            }
            qlen = len(store.get("queue") or [])
            need_refill = qlen < MIN_QUEUE
        else:
            queue: list = list(store.get("queue") or [])
            item = None
            while queue and item is None:
                raw = queue.pop(0)
                if isinstance(raw, dict):
                    item = _normalize_item(raw)
            if item is None:
                item = _pick_fallback()
            store["queue"] = queue
            store["today"] = {
                "date": day_key,
                "hitokoto": item["hitokoto"],
                "from": item.get("from") or "",
                "uuid": item.get("uuid") or "",
            }
            save_store(store)
            need_refill = len(queue) < MIN_QUEUE

    if need_refill:
        maybe_refill_async()
    return item


def get_daily_quote(*, day: date | None = None) -> str:
    """Formatted one-liner for CLI / status."""
    return _format_quote(get_daily_quote_item(day=day))
