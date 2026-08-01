"""Builtin tool schemas, handlers, and tool pool assembly."""

from __future__ import annotations

from harness.agent.cron import run_cancel_cron, run_list_crons, run_schedule_cron
from harness.agent.subagent import run_agent_task
from harness.agents.schema import build_task_tool_schema
from harness.mcp.pool import assemble_tool_pool, connect_mcp
from harness.modes import mode_enables_task, mode_disables_tool
from harness.project.tools import (
    run_project_init,
    run_project_note,
    run_project_reset,
    run_project_set_chapter,
    run_project_status,
)
from harness.rag.tools import run_rag_index, run_rag_search, run_rag_status
from harness.skills_loader import load_skill
from harness.tasks import (
    claim_task,
    clear_active_tasks,
    complete_task,
    create_task,
    get_task_json,
    list_archived_tasks,
    list_tasks,
    reconcile_task_board,
)
from harness.teams import (
    BUS,
    consume_lead_inbox,
    run_request_plan,
    run_request_shutdown,
    run_review_plan,
    spawn_teammate_thread,
)
from harness.tools.filesystem import run_bash, run_edit, run_glob, run_read, run_write
from harness.tools.todo import run_todo_write
from harness.tools.web_search import run_web_search
from harness.todos.schema import TODO_WRITE_TOOL
from harness.worktree import create_worktree, keep_worktree, remove_worktree


def run_create_task(subject: str, description: str = "", blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks(include_archived: bool = False) -> str:
    tasks = list_tasks(include_archived=include_archived)
    if not tasks:
        if include_archived:
            return "No tasks (active or archived)."
        return "No active tasks. Completed work is archived under .tasks/archive/."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks
    )


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


def run_clear_tasks(archive: bool = True) -> str:
    return clear_active_tasks(archive=archive)


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for msg in msgs:
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{msg['type']} req:{req_id}]" if req_id else f" [{msg['type']}]"
        lines.append(f"  [{msg['from']}]{tag} {msg['content'][:200]}")
    return "\n".join(lines)


BUILTIN_TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Long-running commands (installs, builds, "
            "tests, servers) are automatically moved to the background and you "
            "get a task_notification when they finish. For commands that need "
            "more time, pass `timeout` in milliseconds (default 120000, max "
            "3600000). Pass `run_in_background: true` to keep working while a "
            "slow command runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in milliseconds (default 120000, max 3600000).",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run in background and return immediately; result arrives as a task_notification.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read file contents with line numbers and a header showing total lines. "
            "Use `limit`/`offset` to page through large files. Binary files and "
            "paths escaping the workspace are rejected; non-UTF-8 (e.g. GBK) files "
            "are decoded automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return (page through big files).",
                },
                "offset": {
                    "type": "integer",
                    "description": "0-based starting line.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file (creates parent directories). "
            "Content is capped at 2,000,000 chars; write larger files in chunks or via bash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {
                    "type": "string",
                    "description": "File content (max 2,000,000 chars).",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace exact text in a file. If `old_text` appears multiple times, only "
            "the Nth `occurrence` (1-based, default 1) is replaced and the result "
            "reports how many remain. On mismatch, returns a closest-match line hint."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "occurrence": {
                    "type": "integer",
                    "description": "Which occurrence to replace, 1-based (default 1).",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": (
            "Find files matching a glob pattern. Use `**` for recursive matches "
            "(e.g. `**/*.py`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    TODO_WRITE_TOOL,
    {
        "name": "load_skill",
        "description": "Load the full content of a skill by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "compact",
        "description": "Summarize earlier conversation and continue with compacted context.",
        "input_schema": {
            "type": "object",
            "properties": {"focus": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "create_task",
        "description": "Create a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "blockedBy": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List active tasks (pending/in_progress). Completed tasks are auto-archived "
            "and hidden unless include_archived=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"include_archived": {"type": "boolean"}},
            "required": [],
        },
    },
    {
        "name": "clear_tasks",
        "description": (
            "Clear the active task board when starting fresh work. "
            "Archives remaining tasks by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"archive": {"type": "boolean"}},
            "required": [],
        },
    },
    {
        "name": "get_task",
        "description": "Get full task details.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "claim_task",
        "description": "Claim a pending task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "complete_task",
        "description": "Complete an in-progress task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "schedule_cron",
        "description": (
            "Schedule a cron job. cron is 5-field: min hour dom month dow. "
            "For one-shot reminders, compute the target minute and set recurring=false."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron": {"type": "string"},
                "prompt": {"type": "string"},
                "recurring": {"type": "boolean"},
                "durable": {"type": "boolean"},
            },
            "required": ["cron", "prompt"],
        },
    },
    {"name": "list_crons", "description": "List registered cron jobs.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {
        "name": "cancel_cron",
        "description": "Cancel a cron job by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "spawn_teammate",
        "description": "Spawn an autonomous teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    {
        "name": "send_message",
        "description": "Send message to a teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "check_inbox",
        "description": "Check inbox for messages and protocol responses.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "request_shutdown",
        "description": "Request a teammate to shut down.",
        "input_schema": {
            "type": "object",
            "properties": {"teammate": {"type": "string"}},
            "required": ["teammate"],
        },
    },
    {
        "name": "request_plan",
        "description": "Ask a teammate to submit a plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "teammate": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["teammate", "task"],
        },
    },
    {
        "name": "review_plan",
        "description": "Approve or reject a submitted plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["request_id", "approve"],
        },
    },
    {
        "name": "create_worktree",
        "description": "Create an isolated git worktree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remove_worktree",
        "description": "Remove a worktree. Refuses if changes exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "discard_changes": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "keep_worktree",
        "description": "Keep a worktree for manual review.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "connect_mcp",
        "description": "Connect to an MCP server from config/mcp.json or mock servers (docs, deploy).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the public web and return title/url/snippet results. "
            "Chinese queries use 360/so.com first; English uses Bing RSS. "
            "Use this FIRST for「搜一下 / 查找 / 有哪些 / who is」. "
            "Do NOT fetch google.com/baidu.com search pages (robots/captcha). "
            "After search, open a specific promising URL with mcp__fetch__fetch "
            "if you need the page body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query in the user's language",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max hits to return (1–8, default 5)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "rag_index",
        "description": (
            "Index local reference documents for RAG (.md/.txt/.docx/.pdf). "
            "PDF tables are indexed as text; images require opt-in VLM "
            "configuration for visual descriptions and may incur API cost. "
            "Default path empty = files/样例; use path=\"files\" for the full corpus. "
            "Builds hybrid lexical + vector index under .rag/."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory to index. Empty = files/样例",
                }
            },
            "required": [],
        },
    },
    {
        "name": "rag_search",
        "description": (
            "Hybrid search (BM25 + embeddings) on child chunks; returns parent "
            "section context when available. Use before writing thesis/report "
            "sections. By default respects the current /rag document selection; "
            "an explicit source overrides that scope. Requires rag_index first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "source": {
                    "type": "string",
                    "description": "Optional source filename filter",
                },
                "chapter": {
                    "type": "string",
                    "description": "Optional chapter title filter (exact match)",
                },
                "include_captions": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "rag_status",
        "description": "Show local RAG index status: sources, chunk counts, embedding model.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "project_status",
        "description": "Show thesis/report rewrite progress (chapters, output files, resume state).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "project_init",
        "description": "Initialize or reset the thesis rewrite project chapter list and paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "source_doc": {"type": "string"},
                "reset": {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "project_set_chapter",
        "description": "Mark a chapter pending/in_progress/done and set as current.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chapter_id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                "path": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["chapter_id"],
        },
    },
    {
        "name": "project_note",
        "description": "Save free-form notes on the rewrite project (persisted across sessions).",
        "input_schema": {
            "type": "object",
            "properties": {"notes": {"type": "string"}},
            "required": ["notes"],
        },
    },
    {
        "name": "project_reset",
        "description": "Clear saved conversation history only (keeps chapter progress).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

BUILTIN_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "load_skill": load_skill,
    "create_task": run_create_task,
    "list_tasks": run_list_tasks,
    "clear_tasks": run_clear_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "spawn_teammate": spawn_teammate_thread,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_worktree": create_worktree,
    "remove_worktree": remove_worktree,
    "keep_worktree": keep_worktree,
    "connect_mcp": connect_mcp,
    "web_search": run_web_search,
    "rag_index": run_rag_index,
    "rag_search": run_rag_search,
    "rag_status": run_rag_status,
    "project_status": run_project_status,
    "project_init": run_project_init,
    "project_set_chapter": run_project_set_chapter,
    "project_note": run_project_note,
    "project_reset": run_project_reset,
}


def get_tool_pool() -> tuple[list[dict], dict]:
    tools = [
        tool
        for tool in BUILTIN_TOOLS
        if not mode_disables_tool(tool.get("name", ""))
    ]
    handlers = {
        name: handler
        for name, handler in BUILTIN_HANDLERS.items()
        if not mode_disables_tool(name)
    }
    if mode_enables_task():
        insert_at = next(
            (i for i, tool in enumerate(tools) if tool.get("name") == "todo_write"),
            len(tools),
        ) + 1
        tools.insert(insert_at, build_task_tool_schema())
        handlers["task"] = run_agent_task
    return assemble_tool_pool(tools, handlers)
