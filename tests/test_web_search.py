"""Smoke tests for built-in web_search."""

from __future__ import annotations

from unittest import mock

from harness.tools.web_search import SearchHit, run_web_search


def test_web_search_empty_query() -> None:
    assert "empty" in run_web_search("").lower()


def test_web_search_formats_hits() -> None:
    hits = [
        SearchHit("南信大人工智能学院导师名单", "https://faculty.nuist.edu.cn/a", "硕士导师介绍"),
        SearchHit("学院官网", "https://example.com/b", ""),
    ]
    with mock.patch("harness.tools.web_search._search_so", return_value=hits):
        out = run_web_search("南信大 导师", max_results=5)
    assert "web_search (so)" in out
    assert "南信大人工智能学院导师名单" in out
    assert "https://faculty.nuist.edu.cn/a" in out
    assert "硕士导师介绍" in out


def test_web_search_falls_back_when_so_empty() -> None:
    hits = [SearchHit("南信大导师名录", "https://bing.example/x", "")]
    with mock.patch("harness.tools.web_search._search_so", return_value=[]):
        with mock.patch("harness.tools.web_search._search_bing_rss", return_value=hits):
            out = run_web_search("南信大导师")
    assert "web_search (bing)" in out
    assert "南信大导师名录" in out


def test_web_search_in_builtin_pool() -> None:
    from harness.tools.registry import BUILTIN_HANDLERS, BUILTIN_TOOLS

    names = {t["name"] for t in BUILTIN_TOOLS}
    assert "web_search" in names
    assert "web_search" in BUILTIN_HANDLERS


def test_lookup_constraint_mentions_web_search() -> None:
    from harness.prompts.lookup import LOOKUP_CONSTRAINT, is_lookup_query

    assert is_lookup_query("搜一下南京信息工程大学人工智能学院的硕士导师有谁")
    assert "web_search" in LOOKUP_CONSTRAINT


def test_browse_destructive_annotation_requires_permission() -> None:
    from harness.hooks import permission_hook
    from harness.mcp.pool import mcp_tool_meta

    name = "mcp__playwright__browser_navigate"
    mcp_tool_meta[name] = {"destructive": True, "server": "playwright", "tool": "browser_navigate"}
    try:
        block = {"name": name, "input": {"url": "https://www.baidu.com"}}
        with mock.patch("harness.hooks.ask_permission") as ask:
            ask.return_value = mock.Mock(
                decision="deny", allowed=False, remember_session=False,
                remember_always=False, value="",
            )
            assert permission_hook(block) is not None
            ask.assert_called_once()
    finally:
        mcp_tool_meta.pop(name, None)
