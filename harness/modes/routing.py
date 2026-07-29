"""Auto-route user messages to sub-agents in orchestration mode.

When auto_route is enabled (config/modes.json), the agent loop calls
route_user_message() BEFORE the main LLM invocation. The function:

1. Classifies the latest user message into explore / code / write
2. Spawns a sub-agent teammate with a task-specific prompt
3. Waits for the result (via BUS inbox)
4. Injects the summary into the message list for the lead to review
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from harness.modes.runtime import mode_auto_route
from harness.settings import ROUTE_IDLE_TIMEOUT, ROUTE_MAX_RUNTIME
from harness.teams.bus import BUS
from harness.teams.protocol import run_request_shutdown
from harness.teams.teammate import spawn_teammate_thread

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_CONFIG_PATH = PACKAGE_ROOT / "config" / "agents.json"

# Backwards-compatible alias used by callers and tests. A route now tracks worker
# progress and uses this value as its idle deadline, not its total deadline.
ROUTE_TIMEOUT = ROUTE_IDLE_TIMEOUT

# Routing rules: (agent_type, priority, [regex patterns])
_ROUTE_RULES = [
    (
        "explore",
        50,
        [
            r"查看",
            r"检查",
            r"浏览",
            r"登录",
            r"ssh\b",
            r"远程",
            r"读取.*文件",
            r"目录结构",
            r"项目.*结构",
            r"分析",
            r"调研",
            r"梳理",
            r"熟悉.*项目",
            r"搜索",
            r"查找",
            r"查一下",
            r"read.*file",
            r"list.*dir",
            r"cat\s+",
            r"ls\s+",
            r"ping\b",
            r"看.*日志",
            r"错误日志",
            r"配置文件",
            r"配置.*服务",
        ],
    ),
    (
        "code",
        40,
        [
            r"实现",
            r"修改",
            r"编写",
            r"写.*代码",
            r"写.*接口",
            r"写.*API",
            r"写.*函数",
            r"修复",
            r"implement",
            r"fix\b",
            r"add.*feature",
            r"refactor",
            r"添加.*功能",
            r"添加.*模块",
            r"删除.*文件",
            r"创建.*文件",
            r"创建.*模块",
            r"改.*bug",
            r"开发",
            r"编码",
        ],
    ),
    (
        "write",
        30,
        [
            r"写.*文档",
            r"写.*报告",
            r"写.*说明",
            r"文档.*更新",
            r"write.*doc",
            r"撰写",
            r"写.*README",
            r"生成.*文档",
        ],
    ),
]


def load_agents_config() -> dict:
    """Load config/agents.json."""
    if not AGENTS_CONFIG_PATH.exists():
        return {"agents": {}}
    return json.loads(AGENTS_CONFIG_PATH.read_text(encoding="utf-8"))


def _classify_message(text: str) -> str | None:
    """Classify user message into an agent type (explore / code / write / None)."""
    text_lower = text.lower()
    matches: list[tuple[int, str]] = []
    for agent_type, priority, patterns in _ROUTE_RULES:
        for pattern in patterns:
            if re.search(pattern, text_lower):
                matches.append((priority, agent_type))
                break
    if not matches:
        return None
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[0][1]


def _build_agent_prompt(agent_type: str, user_message: str) -> str:
    """Build the instruction prompt for the sub-agent."""
    agents_config = load_agents_config()
    agent_info = agents_config.get("agents", {}).get(agent_type, {})
    system = agent_info.get(
        "system",
        f"You are the {agent_type} worker. Complete the assigned task.",
    )

    return (
        f"## Role\n{system}\n\n"
        f"## User Request\n{user_message}\n\n"
        f"## Instructions\n"
        f"1. Complete the task above to the best of your ability.\n"
        f"2. When finished, call **send_message(to='lead')** with a concise summary "
        f"of what you did and what you found.\n"
        f"3. Do NOT ask for confirmation before taking action.\n"
        f"4. Do NOT spawn sub-agents yourself."
    )


def _teammate_name(agent_type: str) -> str:
    """Generate a unique name for the spawned teammate."""
    suffix = int(time.time() * 1000) % 100000
    return f"route_{agent_type}_{suffix}"


def route_user_message(messages: list) -> bool:
    """Analyze the latest user message and route to a sub-agent if appropriate.

    Called from the agent loop *before* the main LLM call.
    Returns True when routing was performed (messages list may have been modified).
    Returns False when no routing was needed — caller should proceed as normal.
    """
    if not mode_auto_route():
        return False
    if not messages:
        return False

    # Find the most recent plain-text user message (skip internal/system messages)
    last_user: str | None = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                last_user = content.strip()
                break

    if not last_user:
        return False

    # Skip system-injected / harness-internal messages
    if last_user.startswith("[") or last_user.startswith("<") or last_user.startswith("{"):
        return False

    agent_type = _classify_message(last_user)
    if not agent_type:
        return False

    # Spawn the teammate
    prompt = _build_agent_prompt(agent_type, last_user)
    name = _teammate_name(agent_type)

    spawn_teammate_thread(name, f"{agent_type} worker", prompt)

    # Poll BUS for a terminal outcome. Progress messages refresh the idle clock;
    # the absolute cap remains bounded so a healthy-but-never-ending worker cannot
    # consume a turn indefinitely.
    started_at = time.monotonic()
    last_progress_at = started_at
    deferred_messages: list[dict] = []

    def append_deferred_messages() -> None:
        if not deferred_messages:
            return
        messages.append(
            {
                "role": "user",
                "content": "[Inbox]\n" + "\n".join(
                    f"From {msg.get('from', 'unknown')} [{msg.get('type', 'message')}]: "
                    f"{msg.get('content', '')[:200]}"
                    for msg in deferred_messages
                ),
            }
        )

    while time.monotonic() - started_at < ROUTE_MAX_RUNTIME:
        inbox = BUS.read_inbox("lead")
        for msg in inbox:
            if msg.get("from") != name:
                deferred_messages.append(msg)
                continue
            msg_type = msg.get("type")
            if msg_type == "progress":
                last_progress_at = time.monotonic()
                continue
            if msg_type == "result":
                summary = msg.get("content", "[No summary provided]")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Auto-routed to {agent_type} agent]\n"
                            f"Teammate '{name}' completed the task.\n\n"
                            f"## Results\n{summary}\n\n"
                            f"(Lead: review the results above and present "
                            f"a concise summary to the user.)"
                        ),
                    }
                )
                append_deferred_messages()
                return True
            if msg_type in {"error", "timeout"}:
                detail = msg.get("content", "[No details provided]")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Auto-route] {agent_type} agent '{name}' ended with "
                            f"{msg_type}.\n\n## Details\n{detail}\n\n"
                            "(Lead: take over using the failure details above.)"
                        ),
                    }
                )
                append_deferred_messages()
                return True
        if time.monotonic() - last_progress_at >= ROUTE_IDLE_TIMEOUT:
            break
        time.sleep(0.5)

    # Request cooperative shutdown before the lead touches the same workspace.
    run_request_shutdown(name)
    elapsed = round(time.monotonic() - started_at, 1)
    messages.append(
        {
            "role": "user",
            "content": (
                f"[Auto-route] {agent_type} agent '{name}' stopped reporting progress "
                f"after {elapsed}s. A shutdown was requested. Handle the request directly, "
                "but inspect the workspace before overwriting any concurrent changes."
            ),
        }
    )
    append_deferred_messages()
    return True
