from harness.agent.question_state import (
    remember_latest_question,
    pending_question_text,
    remember_turn_question,
)


def test_remember_turn_question_tracks_only_latest_user_facing_question():
    messages = [
        {"role": "user", "content": "帮我处理配置"},
        {"role": "assistant", "content": [{"type": "text", "text": "目标文件是哪一个？"}]},
    ]
    context = {}
    remember_turn_question(context, messages, 0)
    assert pending_question_text(context) == "目标文件是哪一个？"


def test_non_question_turn_clears_stale_pending_question():
    messages = [
        {"role": "user", "content": "目标文件是 app.py"},
        {"role": "assistant", "content": [{"type": "text", "text": "好的，我开始检查。"}]},
    ]
    context = {"pending_question": {"text": "目标文件是哪一个？"}}
    remember_turn_question(context, messages, 0)
    assert pending_question_text(context) == ""


def test_latest_question_can_be_restored_from_saved_history():
    messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "旧回答。"}]},
        {"role": "user", "content": "继续配置"},
        {"role": "assistant", "content": [{"type": "text", "text": "要使用哪个环境？"}]},
    ]
    context = {}
    remember_latest_question(context, messages)
    assert pending_question_text(context) == "要使用哪个环境？"
