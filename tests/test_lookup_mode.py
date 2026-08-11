"""Tests for lookup-mode detection."""

from harness.prompts.lookup import augment_query, is_lookup_query
from harness.prompts.goal_stickiness import looks_like_slot_fill


def test_plain_lookup_triggers():
    assert is_lookup_query("查找一下 pu keyang 南信大 icml2026 的论文")


def test_feasibility_question_triggers():
    assert is_lookup_query("人工智能学院专任教师题目，你可以爬取到吗？")


def test_english_lookup_triggers():
    assert is_lookup_query("Find paper by Pu Yang at ICML 2026")


def test_code_change_does_not_trigger():
    assert not is_lookup_query("给 harness/loop.py 加一个 max_rounds 参数")


def test_mixed_but_impl_dominated_does_not_trigger():
    # mentions "论文" once but is clearly a code task
    q = "改一下 loop.py 里查找论文的逻辑，fix 那个 repeat guard 的 bug"
    assert not is_lookup_query(q)


def test_augment_preserves_original():
    q = "查找一下 pu keyang 南信大 icml2026 的论文"
    out = augment_query(q)
    assert out.startswith(q)
    assert "[Lookup mode" in out
    assert "成功标准" in out


def test_augment_noop_for_non_lookup():
    q = "给 hooks.py 加一个 lookup 检测"
    out = augment_query(q)
    assert out == q


def test_generic_code_review_question_does_not_trigger_lookup():
    assert not is_lookup_query("这个函数是否线程安全")


def test_short_reply_is_not_a_slot_fill_without_pending_question():
    assert not looks_like_slot_fill("你好")
