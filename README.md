# Improved Harness

基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的 Agent Harness 改进版。

上游是一套「从零拆解 Claude Code 式 harness」的教学仓库；本仓库从其中的综合章 **`s20_comprehensive`**（单文件 `code.py`）出发，拆成可维护的包，并在真实使用中持续修问题、加能力。

本仓库远程：https://github.com/1074739572/claude-code_exchange

---

## 代码从哪来

| 项 | 说明 |
|----|------|
| 上游仓库 | https://github.com/shareAI-lab/learn-claude-code |
| 逻辑基线 | `s20_comprehensive/code.py`（约 2100 行单文件综合版） |
| 教学对应 | s01–s20：loop / tools / hooks / todo / subagent / skills / compact / memory / recovery / tasks / background / cron / teams / worktree / MCP |
| 本仓库做了什么 | 模块化拆包 + 多模型 / RAG / 会话与压缩 / UI 等工程化改进（见下表） |

克隆上游（对照用）：

```sh
git clone https://github.com/shareAI-lab/learn-claude-code
cd learn-claude-code
```

---

## 我们做了哪些改进

相对上游 `s20` 单文件版，以及后续在真实任务里踩坑后的修复：

### 结构与基础设施

| 改进 | 说明 |
|------|------|
| 包结构拆分 | 单文件 → `harness/` 按子系统拆包，便于测试与替换 |
| Skills 自包含 | 自带 `skills/`，不依赖上游仓库根目录 |
| MCP 双模式 | `RealMCPClient`（stdio）+ `MockMCPClient`；配置在 `config/mcp.json` |
| MCP 权限元数据 | 用 `destructive` 等元数据驱动确认，而不是按名字硬匹配 |
| 多模型 Provider | Anthropic + OpenAI 兼容路由；CLI `/model` 选择 |
| 本地 RAG | hybrid BM25+向量、parent-child 切块、`/rag` CLI、`/mode file` 文档问答 | [rag](docs/rag.md) · [CHANGELOG-07-15](docs/CHANGELOG-2026-07-15.md) |
| 项目说明书 | 启动读 `HARNESS.md`（回退 `AGENTS.md`）→ ephemeral；GAIA 默认关闭 | [project-instructions](docs/project-instructions.md) |

### 会话与任务（真实使用踩坑后）

| 改进 | 说明 | 记录 |
|------|------|------|
| Resume OpenCode 模式 | 默认全新会话；`/clear` 一次清干净；`/resume N` 切换会话；续项目需 `/resume project` | [003](docs/bugs/003-resume-opt-in.md) |
| Todo 持久化与提醒 | `sessions/<id>/todos.json`（跟会话）；每轮注入；reminder | [001](docs/bugs/001-todo-drift.md) · [003](docs/bugs/003-resume-opt-in.md) |
| 工具空转护栏 | RepeatGuard；LookupGuard；WritingGuard | [005](docs/bugs/005-tool-loop-drift.md) |
| 任务落锚 | Resolve→Act→Close；GroundingGuard；禁 tool 参数写假设 | [005](docs/bugs/005-tool-loop-drift.md) · [07-15](docs/CHANGELOG-2026-07-15.md) |
| 中断与回滚 | Esc / SIGINT 停当前轮；orphan `tool_use` 修复，避免 API 400 | [001](docs/bugs/001-todo-drift.md) |
| Prompt 缓存分层 | static / dynamic / ephemeral 分离，提高 cache hit | [002](docs/bugs/002-prompt-cache-vs-dynamic-context.md) |
| 项目说明书 | `HARNESS.md` → 否则 `AGENTS.md`；session 启动注入 ephemeral | [project-instructions](docs/project-instructions.md) |
| 上下文压缩 | compact 保留尾部；结构化摘要；micro_compact 落盘；按模型自适应预算 | [004](docs/bugs/004-context-compaction.md) |

### 交互与体验

| 改进 | 说明 |
|------|------|
| 欢迎页 / Session 面板 | Rich UI，中文状态行 |
| 模型 / 模式选择器 | 终端 ↑↓ 菜单；`/mode` 含 direct / file / writing / lookup 等 |
| 工具终端 UI | 只显示步骤 `›`/`●` + 回合末 Changed files；成功结果不刷屏 |
| **Rich 行式 CLI（默认）** | 欢迎页 / 步骤 UI / 权限面板 / 回合文件摘要；`--event-stream` 输出 JSONL | [008](docs/bugs/008-textual-tui-m1.md) |
| 中文 CLI 文案 | `/help`、`/resume`、`/clear`、`/rag` 等 |
| 本地用量统计 | `.project/usage/`；`/usage`；提示符显示当前模型 |
| 评测 | mini-eval / SWE-bench / GAIA validation | [evals](docs/evals.md) |

文档索引：[`docs/`](docs/README.md)。问题与取舍见 [`docs/bugs/`](docs/bugs/README.md)。

相对上游的对照总览见下方「与上游 s20 的 diff」。后续改什么不写死路线图——有新痛点再商量，改完补 bug / changelog 即可。

---

## 与上游 s20 的 diff

上游基线：`learn-claude-code/s20_comprehensive/code.py`（教学用单文件综合 Agent）。

| 维度 | 上游 s20 | 本仓库 improved_harness |
|------|----------|-------------------------|
| 形态 | 约 2k 行单文件 | `harness/` 分包 + `tests/` + `docs/` |
| 模型 | 偏 Anthropic | 多 provider（DeepSeek / Qwen / 智谱…）`/model` |
| 会话 | 演示级 | session 分桶、todos 跟会话、`/resume` 切换删除 |
| 压缩 | 基础 compact | 结构化摘要、落盘可恢复、模型预算、最新用户优先 |
| 护栏 | 基本无 | Repeat / Lookup / Writing / Grounding |
| 文档能力 | 无或极简 | 本地 RAG + file/writing 模式 + `/rag` |
| 终端 | print 为主 | Rich；步骤 UI；用量；中断 |
| Skills | 依赖教学仓布局 | 自带 `skills/`（含 paper-lookup 等） |
| 工程化 | 教学演示 | changelog、bug 编号记录、evals |
---

## 目录结构

```
improved_harness/
├── main.py                 # CLI 入口
├── requirements.txt
├── .env.example
├── config/                 # mcp / agents / modes 配置
├── skills/                 # 内置技能
├── tests/                  # 单元测试
├── docs/
│   ├── README.md           # 文档索引
│   ├── evals.md            # mini-eval / SWE-bench / GAIA 说明
│   ├── bugs/               # 问题与改进记录（001–005…）
│   └── CHANGELOG-*.md      # 阶段性改动总览
├── scripts/                # 实验与文档构建脚本
└── harness/                # 核心包
    ├── loop.py             # agent 主循环
    ├── cli.py              # 交互式 CLI
    ├── settings.py         # 路径与常量
    ├── llm.py / models.py  # 调用与模型表
    ├── context.py          # 记忆等上下文
    ├── hooks.py            # Pre/Post tool hooks
    ├── skills_loader.py
    ├── tasks.py / worktree.py / usage.py
    ├── agent/              # compact/ + recovery / cancel / cron / background
    │   └── compact/        # sizing · messages · persist · layers · summarize · pipeline
    ├── prompts/            # static / dynamic / ephemeral
    ├── project/            # session / resume / clear / undo
    ├── todos/              # todo 状态与格式化
    ├── tools/              # filesystem / registry / dispatch
    ├── providers/          # Anthropic / OpenAI-compat
    ├── rag/                # 检索
    ├── mcp/                # MCP 客户端与池
    ├── teams/              # 多队友
    ├── modes/ / agents/    # 模式与 typed subagent
    ├── messages/           # repair / sanitize
    └── ui/                 # welcome / picker / interrupt
```

运行时落盘（相对工作目录 cwd）：

| 路径 | 内容 |
|------|------|
| `.project/` | `sessions/<id>/`（对话+todos）、`state.json`（长任务）、history |
| `.transcripts/` | compact 前完整备份 |
| `.memory/MEMORY.md` | 长期记忆 |
| `.tasks/` / `.mailboxes/` / `.worktrees/` | 任务图、队友邮箱、worktree |
| `.rag/` | RAG 索引与 chunks |

---

## 快速开始

```sh
cd improved_harness
cp .env.example .env          # 填入 API Key 与 MODEL_ID
pip install -r requirements.txt
python main.py                # 默认 Rich 行式 CLI
python main.py --classic      # 同上（兼容参数，等价默认）
python main.py --event-stream # JSONL 事件流后端（供外部 TUI 客户端）
```

在**你的工作区目录**（cwd）跑 Agent；skills 从本包 `skills/` 加载；会话与任务状态写在 cwd 下的 `.project/` 等目录。

欢迎页：顶部 **hero + 每日一句 + 会话摘要** 再接历史。每日一句队列在 `.project/daily_quotes.json`（Hitokoto），不足 5 条时启动后后台自动补货。

内置斜杠命令：`/help`、`/model`、`/mode`（含 **file** 文档问答）、`/resume`、`/skill`、`/clear`、`/quit`、`/usage`、`/rag`。

`--event-stream` 输出 JSONL 事件（`assistant_delta` / `tool_start` / `permission_request` 等），供外部 TUI 客户端消费；`--event-stream` 后端事件定义见 `harness/event_stream.py`。

```text
› Working goal: 修好 lookup 死循环
● edit_file  harness/loop.py
● read_file  harness/agent/lookup_guard.py
Changed files:
  · harness/loop.py
```

默认只展示 **每一步在做什么**（意图 + 工具名/路径），成功结果不刷屏；完整 tool_result 仍进对话给模型。回合结束列出本轮 `write_file` / `edit_file` 改过的文件。错误与 Guard 拦截仍会显示 `→`。每轮 LLM 的 cache hit/miss、compact 落盘路径默认不打印（避免盖住最终 Assistant 面板）；需要时设 `HARNESS_VERBOSE=1`。

- `HARNESS_VERBOSE=1` 恢复 `[HOOK]` / `[cache]` / `[compact] transcript` 调试行
- 项目说明书：cwd 向上找 `HARNESS.md` / `AGENTS.md`（`HARNESS_PROJECT_MD=0` 关闭）
- 同一工具+相同参数连续调用满 3 次会被拦截（`HARNESS_REPEAT_LIMIT`），避免死循环刷屏
- 查找题（lookup mode）：**默认无联网次数硬顶**；仍拦近重复搜索、连续无效搜索、失败 URL/host（`HARNESS_LOOKUP_*`）。若要硬顶：`HARNESS_LOOKUP_FETCH_LIMIT=N`

提示符形如 `[qwen-max] >`。`/usage` 查看今日输入/输出/命中率（字符直方图）；`/usage week|month|year` 看历史。数据在 `.project/usage/`，`/clear` 不会删。

## 评测（mini-eval / SWE-bench / GAIA）

本地能力回归（评 harness 接线，不评「聪不聪明」）：

```sh
python -m evals
python -m evals --live
```

SWE-bench Lite（克隆仓库 → 跑本 harness → 产出 patch）：

```sh
pip install -r requirements-eval.txt
python -m evals.swebench --limit 1
python -m evals.swebench --limit 1 --eval   # 需 Docker Desktop；官方 resolve 打分
```

GAIA validation（有标准答案；官方 quasi-exact match）：

```sh
python -m evals.gaia --download --validation-only
python -m evals.gaia --limit 3
python -m evals.gaia --level 1 --limit 10
```

结果在 `evals/results/`。说明见 [docs/evals.md](docs/evals.md)。

## MCP（可选插件）

`config/mcp.json` 已预置两个公用 server（启动时自动连接；成功静默，失败才告警）：

| 名字 | 作用 | 本机需要 |
|------|------|----------|
| **fetch** | 拉网页转 markdown | `pip install -r requirements.txt`（含 `mcp-server-fetch`） |
| **playwright** | 浏览器自动化 | 已装 **Node.js**（首次 `npx` 会拉包） |

连上后工具名形如 `mcp__fetch__fetch`、`mcp__playwright__…`。某个 server 缺依赖时只会在启动日志里报失败，不影响其它能力。不想用某个时，从 `mcp.json` 删掉对应条目即可。

---

## 与上游 s20 的对照（一眼）

| 项 | 上游 `s20_comprehensive/code.py` | 本仓库 |
|----|----------------------------------|--------|
| 结构 | 单文件 | `harness/` 分包 |
| Skills | 依赖仓库根 `skills/` | 自带 `skills/` |
| MCP | 多为 mock | mock + 真实 stdio |
| 模型 | 偏 Anthropic 教学默认 | 多 Provider + `/model` |
| 会话 | 教学向 | OpenCode 默认全新 + opt-in resume |
| 压缩 | 基础四层 | 尾部保留 + 结构化摘要 + tool 落盘 |

更细的模块职责见 [ARCHITECTURE.md](ARCHITECTURE.md)。
