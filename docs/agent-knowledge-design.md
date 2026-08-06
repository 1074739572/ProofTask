# 项目指令文件（HARNESS.md/AGENTS.md）：调研与落地设计

> 状态：调研完成；`/init` 已实现（2026-08-06，MVP：只读 explore 子代理扫描 → 生成/就地改进 HARNESS.md，旧版备份到 `.local/init-backup/`，I001–I004 零 LLM 评测）。`@import` 与 MEMORY.md 沉淀**未实现**。
> 日期：2026-08-06
> 调研问题：① 各家 agent 怎么初始化生成指令 md；② 后续知识怎么持续加入。

## 1. 各家做法对比

### 1.1 初始化

| 工具 | 方式 | 要点 |
|---|---|---|
| Claude Code | `/init`（LLM 扫描生成） | 扫描代码库生成 CLAUDE.md（构建/测试命令、约定）；**已存在则提改进建议、不覆盖**；`CLAUDE_CODE_NEW_INIT=1` 多阶段（选产物→子代理探索→追问→可审阅提案）；吸收 Cursor/Copilot/AGENTS.md 规则 |
| opencode | `/init`（LLM 扫描+提问） | 扫描重要文件、必要时提问，生成或**就地改进** AGENTS.md |
| Codex | 无 | 纯手动创建 |
| Cursor | `/create-rule` | LLM 按聊天描述生成带 frontmatter 的 `.cursor/rules/*.mdc`；AGENTS.md 纯手动 |
| Aider | 无 | 手动创建 + `/read` 或配置加载 |
| Windsurf | 无 | 手动创建；AGENTS.md 按目录位置自动定作用域 |
| Gemini CLI | 无 | 手动创建 GEMINI.md |

### 1.2 后续知识更新

| 工具 | 手动 | 让 agent 代写 | 自动沉淀 | 引用外部文件 |
|---|---|---|---|---|
| Claude Code | ✅ `/memory` | ✅ 说 "add this to CLAUDE.md" | ✅ **auto memory**（默认开：`~/.claude/projects/<p>/memory/`，MEMORY.md 前 200 行/25KB 每次加载，自裁剪；`/compact` 后根 CLAUDE.md 自动重注入） | ✅ CLAUDE.md 内 `@path` import（递归≤4层）；`@AGENTS.md` 一行导入 |
| opencode | ✅ | ✅ | ❌ | ✅ `instructions` glob/URL（推荐）；AGENTS.md 内 @引用需自写指令按需 Read |
| Codex | ✅ 每次运行重扫 | ❌ | ❌ | ⚠️ 仅配置化 fallback 文件名；`AGENTS.override.md` 临时覆盖 |
| Cursor | ✅ Customize 面板 | ✅ 聊天让 agent 写 | ❌ | ✅ `@filename`；GitHub 远程导入 |
| Aider | ✅ 改 md/配置 | ❌ | ❌ | ✅ `.aider.conf.yml read:` 多文件 |
| Windsurf | ✅ Rules 面板 | ✅ | ✅ 自动 Memories（官方建议转规则） | ✅ 规则 @-mention |
| Gemini CLI | ✅ | ❌ | ⚠️ 独立 Memory tool | ✅ GEMINI.md `@import` |

**结论**：初始化只有 Claude Code / opencode 提供 LLM 扫描的 `/init`；持续知识的主流是「手动 + 让 agent 代写 + 引用外部文件」，只有 Claude Code / Windsurf 有自动沉淀。

## 2. 本仓库现状（improved_harness）

- **加载端已有**：`harness/prompts/project_md.py` — 启动时从工作目录向上查找 `HARNESS.md` > `AGENTS.md`（最近优先、首个命中），12k 字符截断（`HARNESS_PROJECT_MD_MAX_CHARS`），注入 context（M004/M006 已测）。
- **长期记忆已有（手动）**：`.memory/MEMORY.md` — `context.py` 启动加载前 2000 字符注入；无写入命令、无自动沉淀。
- **缺口**：无 `/init` 生成/改进；无 `@import` 引用展开；无知识追加命令；无自动沉淀。

## 3. 落地设计

### 3.1 已实现：`/init`（2026-08-06，MVP）

- `harness/prompts/init_md.py`：`run_init()` / `handle_init_command()`。
- 流程：只读 `explore` 子代理（`run_agent_task`）扫描仓库 → 返回 HARNESS.md 内容 → 原子写入
  `<workspace>/HARNESS.md`；已有文件时把原文回喂给子代理，要求保留有用内容、修正过期命令、
  补充缺失部分（就地改进，不覆盖）；写入前旧版备份到 `.local/init-backup/`（gitignored）。
- 接线：CLI `/init`（持 `agent_lock`）+ TUI `/init`（走 worker 队列）；goal 运行时拒绝。
- 零 LLM 评测：`evals/cases/project_md.py` I001–I004（创建/就地改进+备份/prompt 回喂/空结果不覆盖）。
- 真实冒烟通过：临时仓库生成完整 HARNESS.md（Commands/Layout/Conventions/DoD）。
- 已知边界：`run_agent_task` 的 `cwd` 只进 system prompt，工具仍按进程活动工作区执行——
  `/init` 只作用于当前工作区（符合预期；未来如需任意目录要改 agents runner）。

三层模型：
```
HARNESS.md         稳定规则（提交 git）：Commands / Layout / Conventions
  └─ @docs/xxx.md  模块化详细文档：启动时展开（学 Claude Code @import，递归≤4层）
.memory/MEMORY.md  运行时经验（已有加载端，首 2000 字符注入）
```

1. **`/init` 命令**（学 opencode/Claude Code）：调现有只读 `explore` 子代理（`run_agent_task`）扫描仓库 → 生成 HARNESS.md；已存在则**就地改进**（合并，不覆盖已有内容）；产出聚焦：构建/测试命令、不明显的架构、约定与坑。
2. **`@import` 展开**：`project_md.py` 加解析 — HARNESS.md 中的 `@path` 行启动时读入并拼接（Claude Code 模式，递归 ≤4 层），实现"引用不复制"；截断预算从 12k 总预算中扣除。
3. **沉淀两条路**：
   - 让 agent 代写：现有 `write_file`/`edit_file` 工具已支持（权限 allow），无需新工具；
   - 自动沉淀挂到 `.memory/MEMORY.md`（已有加载端）：`/remember <内容>` 命令以时间戳块追加，或 goal 完成时沉淀一条；限制长度防膨胀（参考 Claude 自裁剪：超限要求重写索引）。
4. 可选后续：`/doctor` 式瘦身（Claude Code 剪掉可从代码推导的内容、保留坑与约定）。

## 4. 来源

- opencode rules：https://opencode.ai/docs/rules/
- Claude Code memory/habits（镜像同步）：https://github.com/victor-software-house/claude-code-docs/blob/main/docs/en/memory.md
- Codex AGENTS.md（镜像）：https://github.com/life-is-blue/openai-codex-docs-mirror/blob/main/docs/codex/agent-configuration/agents-md.md ；加载实现 `codex-rs/core/src/agents_md.rs`
- Cursor rules：https://docs.cursor.com/context/rules
- Aider conventions：https://aider.chat/docs/usage/conventions.html
- Windsurf AGENTS.md/Memories：https://docs.windsurf.com/windsurf/cascade/agents-md.md 、https://docs.windsurf.com/windsurf/cascade/memories.md
- Gemini CLI GEMINI.md：https://www.geminicli.com/docs/cli/gemini-md.md
