# 项目说明书（HARNESS.md / AGENTS.md）

Harness 在**用户工作区**启动会话时，会读一份项目手册并注入 ephemeral context（不是产品人格，也不是 Cursor 的 `.cursor/rules`）。

对齐决策见对话 grill；行为摘要如下。

---

## 发现规则

1. 从 `WORKDIR`（进程 cwd）**向上**查找，最多 `HARNESS_PROJECT_MD_MAX_DEPTH`（默认 10）层  
2. 每个目录：**先** `HARNESS.md`，否则 `AGENTS.md`  
3. **最近一份赢**；有 `HARNESS.md` 时不读 `AGENTS.md`（不拼接）  
4. 遇到含 `.git` 的目录后停止继续向上  
5. Session **启动读一次**；`/clear` 会按当前 cwd 再读一次；**不热重载**磁盘改动  

关闭：`HARNESS_PROJECT_MD=0`  
体积上限：`HARNESS_PROJECT_MD_MAX_CHARS`（默认 12000）；超限硬截断，UI 显示 `[truncated]`  

GAIA eval 默认关闭加载，避免本仓库手册污染分数。

---

## 写什么 / 不写什么

| 写 | 不写 |
|----|------|
| Overview / Commands / Layout / Conventions / Safety | 「你是 Harness…」人格 |
| 可复制的 build/test/lint 命令 | 工具清单（bash、web_search…） |
| 目录边界、「别改」区域 | Mode 纪律（Resolve→Act→Close） |
| 安全红线 | 整本长文架构（只写路径，需要时再 `read_file`） |

无 `@path` 展开（v1）；无 `HARNESS.local.md`（v1）。

---

## 多余文件存放规则

1. 所有临时生成的测试脚本、调试文件、一次性工具等**与项目无关**的文件，必须放入 **`_scraps/`** 目录下
2. 不得在项目根目录、`harness/`、`config/`、`tests/` 等核心目录下产生零散文件
3. `_scraps/` 目录中的文件**不提交**（已加入 `.gitignore`），随时可清理
4. 示例用法：`_scraps/my_debug_script.py`

---

## 空模板（复制为仓库根目录 `HARNESS.md`）

```markdown
# Overview

<!-- 3–8 行：这是什么项目、给谁用 -->

# Commands

<!-- 可复制命令 -->
- Build:
- Test:
- Lint:
- Run:

# Layout

<!-- 关键目录；哪些路径不要随意改 -->

# Conventions

<!-- 命名、错误处理、测试、commit/PR 习惯 -->

# Safety

<!-- 绝对不要：push 保护分支、碰生产密钥、删库等 -->
```

没有 `HARNESS.md` 时，若存在 [AGENTS.md](https://agents.md/) 会作为回退被加载。

---

## 注入位置

- Context 键：`project_instructions` / `project_instructions_source` / `…_truncated` / `…_status`  
- Ephemeral 块：`<project-instructions source="HARNESS.md">…</project-instructions>`  
- **不进** `harness/prompts/sections.py` 的 identity  

启动时 UI 会显示一行状态，例如：

- `project instructions: HARNESS.md`  
- `project instructions: AGENTS.md [truncated]`  
- `no project instructions`  
- `project instructions: disabled`  

代码入口：`harness/prompts/project_md.py`。
