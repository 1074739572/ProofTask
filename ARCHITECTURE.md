# Architecture

## 来源

- 上游：https://github.com/shareAI-lab/learn-claude-code
- 逻辑基线：`s20_comprehensive/code.py`
- Skills：从上游 `skills/` 复制后按需增补
- 本仓库：https://github.com/1074739572/claude-code_exchange

已做改进清单见 [README.md](README.md)；踩坑与修复细节见 [docs/bugs/](docs/bugs/README.md)。

---

## 设计原则

**机制很多，循环一个。** 所有能力挂在同一个 `agent_loop` 上，与上游 s01–s20 教学一致。

```
用户输入 → hooks → cron/background 注入 → compact → system prompt → LLM
    → tool_use? → PreToolUse → dispatch → PostToolUse → messages → 下一轮
```

Prompt 侧再拆一层：

```
static system（可缓存） + ephemeral user 消息（todos / 时间 / 最新意图等）
```

---

## 模块职责

| 模块 | 对应教学章 | 职责 |
|------|-----------|------|
| `loop.py` | s01 | 主循环、tool_result 回写 |
| `tools/registry.py` | s02 | 工具 schema + handler 分发 |
| `hooks.py` | s03–s04 | 权限与扩展点 |
| `todos/` + `tools/todo.py` | s05 | 会话内 todo（持久化到 `sessions/<id>/todos.json`） |
| `agent/subagent.py` / `agents/` | s06 | 一次性子 Agent / typed 角色（实现在 `agents/`，`agent.subagent` 兼容 re-export） |
| `skills_loader.py` | s07 | 按需技能 |
| `agent/compact/` | s08 | 分层压缩（sizing / messages / persist / layers / summarize / pipeline） |
| `context.py` + `prompts/` | s09–s10 | 记忆 + static/dynamic/ephemeral |
| `agent/recovery.py` | s11 | 错误恢复 |
| `tasks.py` | s12 | 持久化任务图 |
| `agent/background.py` | s13 | 后台 bash |
| `agent/cron.py` | s14 | 定时调度 |
| `teams/*` | s15–s17 | 邮箱、协议、自治队友；运行时行为见 [docs/teammate-lifecycle.md](docs/teammate-lifecycle.md) |
| `worktree.py` | s18 | 目录隔离 |
| `mcp/*` | s19 | MCP 发现与调用 |
| `providers/` / `rag/` / `project/` / `ui/` | — | 本仓库扩展：多模型、检索、会话、终端 UI |
| 工具注册·权限·护栏（说明文档） | s02 + 扩展 | 见 [docs/tools.md](docs/tools.md) |

---

## 数据落盘（相对 cwd）

| 路径 | 内容 |
|------|------|
| `.tasks/` | 活动任务（pending / in_progress） |
| `.tasks/archive/` | 已完成/已取消任务 |
| `.mailboxes/` | 团队消息 JSONL |
| `.worktrees/` | git worktree |
| `.memory/MEMORY.md` | 长期记忆 |
| `.project/` | 见下表「会话与长任务」 |
| `.project/usage/` | 按日 token / 缓存命中流水（`/usage`；`/clear` 不删） |
| `.transcripts/` | compact 前完整备份 |
| `.rag/` | RAG corpus / chunks |
| `.scheduled_tasks.json` | 持久 cron |

### 会话与长任务（`.project/`）

| 路径 | 层 | 内容 |
|------|-----|------|
| `active_session.json` | — | 当前会话 id |
| `sessions/<id>/session.jsonl` | A | 对话历史 |
| `sessions/<id>/session.meta.json` | A | 标题、时间、游标 |
| `sessions/<id>/todos.json` | A | 本会话 `todo_write` |
| `state.json` | B | 长任务档案（论文章节等，单槽） |
| `usage/` | — | token 流水（`/clear` 不删） |
| `history.json` | — | 会话列表元数据 |

- **A**：跟 session；`/clear` 结束当前 id，旧目录保留；`/resume N` 切换。  
- **B**：`/resume project` 显式注入；`/clear` 默认删除。  

详见 [docs/bugs/003-resume-opt-in.md](docs/bugs/003-resume-opt-in.md)。

---

## 依赖关系（简图）

```mermaid
flowchart TB
    CLI[cli.py] --> LOOP[loop.py]
    LOOP --> COMPACT[agent/compact/]
    LOOP --> RECOVERY[agent/recovery.py]
    LOOP --> REGISTRY[tools/registry.py]
    REGISTRY --> MCP[mcp/pool.py]
    REGISTRY --> TEAMS[teams/*]
    LOOP --> HOOKS[hooks.py]
    HOOKS --> MCP
    LOOP --> PROMPTS[prompts/]
    PROMPTS --> SKILLS[skills_loader.py]
    CLI --> PROJECT[project/]
    CLI --> UI[ui/]
    REGISTRY --> RAG[rag/]
```
