# 主流 Agent 记忆架构对比

## 1. Cline (VS Code 扩展)

### 短期记忆
- **对话历史**：完整保留 `apiConversationHistory`（API 格式）和 `clineMessages`（UI 格式）
- **上下文压缩**：`ContextManager` 根据模型动态调整
  - DeepSeek: 64K，保留 27K 缓冲
  - Claude: 200K，保留 40K 缓冲
  - **策略**：中等压力删除一半对话，严重压力删除四分之三
  - **智能保留**：始终保留原始任务消息和用户-助手对话结构

### 长期记忆
- **任务持久化**：每个任务有独立存储目录，保存完整对话历史
- **Git 检查点**：每次工具执行后创建 Git 提交，可回滚到任意检查点
- **状态恢复**：任务可从任意点恢复，中断的工具调用会被标记为"任务中断前此工具调用未完成"
- **Hook 系统**：`PreCompact` Hook 可在压缩前注入上下文（规划中）

### 特点
- 状态完全持久化到 VSCode 存储
- 支持跨会话恢复，但依赖完整的对话历史
- 没有独立的"知识库"概念，所有上下文都在对话中

### 关键代码结构
```
src/core/task/index.ts          # Task 类，管理对话和工具执行
src/core/context/ContextManager.ts  # 上下文压缩管理
```

---

## 2. Codex (OpenAI CLI)

### 短期记忆
- **Thread 机制**：每个会话是一个 Thread，支持 `thread/compact/start` 手动压缩
- **上下文管理**：严格控制上下文大小
  - 规则："No history rewrite - context must be built up incrementally"
  - 规则："No unbounded items - everything must have a bounded size and hard cap"
- **Ephemeral 模式**：支持 `thread.ephemeral = true` 的内存-only 会话

### 长期记忆
- **两阶段记忆管道**：
  - **Phase 1（提取）**：从每个会话（rollout）提取结构化记忆
    - 并行处理多个会话（有并发上限）
    - 输出：`raw_memory`（详细）、`rollout_summary`（摘要）、`rollout_slug`（可选）
    - 自动脱敏（secrets redaction）
    - 存储到状态数据库
  - **Phase 2（整合）**：全局整合到文件系统
    - 生成 `raw_memories.md`（合并的原始记忆）
    - 生成 `rollout_summaries/`（每个会话的摘要文件）
    - 运行专门的整合 Agent 更新 `MEMORY.md` 和 `memory_summary.md`
    - 使用 Git 管理记忆工作区的变更
- **记忆读取**：读取时注入开发者指令和记忆引用

### 特点
- 记忆生成是**异步后台任务**，在会话启动时触发
- 记忆存储在 `~/.codex/memories/`，使用 Git 管理版本
- 有完整的**记忆生命周期管理**：生成、整合、清理过期记忆
- 支持 `memory/reset` 清除所有记忆

### 关键代码结构
```
codex-rs/memories/               # 记忆管道
  read/                          # 读取路径（注入、引用解析）
  write/                         # 写入路径（Phase 1/2 prompt 渲染）
codex-rs/core/src/memories/      # 运行时编排
codex-rs/app-server/             # Thread 管理和压缩 API
```

### 记忆触发条件
- 会话不是 ephemeral
- 记忆功能已启用
- 不是子 agent 会话
- 状态数据库可用

---

## 3. ProofTask（本系统）

### 短期记忆
- **Worker 对话**：每个 Worker 有完整的 `messages` 列表
- **自动压缩**：`prepare_context()` 检测 token 接近上限（~83.5% context window）时触发
- **压缩策略**：用 LLM 摘要旧消息，只保留最近几轮完整对话
- **降级保护**：摘要失败时保留近期消息而非给出空摘要

### 长期记忆
- **Goal 状态机**：`goal.json` 记录整体进度
- **决策日志**：`decisions.jsonl` 记录关键策略判断（最近 12 条注入新 Worker）
- **Worker 交接**：`handoff.json` 传递结构化事实（不传聊天记录）
- **测试绑定**：`test-map.json` 记录测试与 Task 的映射
- **Task 完整记录**：每个 Task 的验收条件、evidence、修复历史
- **项目知识**：`MEMORY.md` 存储跨 Goal 的项目知识（≤2000 字符）

### 特点
- Worker 是**一次性消耗品**，到点就换新的
- 所有需要跨 Worker 传递的信息都以**结构化 JSON 持久化**
- 新 Worker 通过 prompt 注入来"回忆"，不依赖聊天历史
- `_resume_target()` 智能推算恢复点，不盲目跳到上次的 phase

### 关键代码结构
```
harness/goal/runner.py           # Goal 运行器，管理 Worker 生命周期
harness/goal/memory.py           # 决策日志、Worker 交接、测试绑定
harness/goal/prompt.py           # prompt 组装，注入长期记忆
harness/agent/compact/pipeline.py # 上下文压缩
harness/context.py               # 运行时上下文，加载 MEMORY.md
```

---

## 4. Claude Code (Anthropic CLI)

### 短期记忆
- **上下文窗口**：200K tokens
- **自动压缩**：接近上限时自动压缩
- **CLAUDE.md**：项目级指令文件，每轮注入

### 长期记忆
- **CLAUDE.md**：项目级知识（类似 MEMORY.md）
- **对话历史**：完整保留，支持跨会话恢复
- **没有独立的记忆管道**：依赖对话历史和 CLAUDE.md

### 特点
- 简单直接，没有复杂的记忆管理
- 依赖模型的长上下文能力
- 用户手动维护 CLAUDE.md

---

## 5. OpenHands (Agent Canvas)

### 短期记忆
- **会话管理**：每个 agent 有独立的会话
- **上下文控制**：通过配置控制上下文大小

### 长期记忆
- **工作区持久化**：文件系统变更持久化
- **检查点系统**：支持回滚到之前的检查点
- **没有独立的记忆管道**：依赖文件系统和会话状态

### 特点
- 专注于 agent 编排而非记忆管理
- 支持多种 agent 后端（Claude Code、Codex 等）
- 记忆管理委托给具体的 agent 实现

---

## 对比总结

| 特性 | Cline | Codex | ProofTask | Claude Code |
|---|---|---|---|---|
| **短期记忆** | 完整对话历史 + 智能压缩 | Thread + 手动压缩 | Worker 对话 + 自动压缩 | 对话历史 + 自动压缩 |
| **长期记忆** | Git 检查点 + 任务存储 | 两阶段记忆管道 | 结构化 JSON + Goal 状态机 | CLAUDE.md + 对话历史 |
| **记忆生成** | 实时保存 | 异步后台任务 | 实时持久化 | 实时保存 |
| **记忆整合** | 无 | Phase 2 整合 Agent | 无（直接注入） | 无 |
| **跨会话恢复** | 支持（依赖完整历史） | 支持（Thread 持久化） | 支持（智能推算恢复点） | 支持（依赖对话历史） |
| **记忆清理** | 无 | 自动清理过期记忆 | Goal 归档 | 无 |
| **知识库概念** | 无 | `MEMORY.md` + `memory_summary.md` | `MEMORY.md` + 决策日志 | `CLAUDE.md` |
| **压缩触发** | 接近上下文上限 | 手动或自动 | 接近 83.5% 上下文上限 | 接近上下文上限 |
| **压缩策略** | 删除一半/四分之三对话 | LLM 摘要 | LLM 摘要 + 降级保护 | 未公开 |
| **记忆存储位置** | VSCode 存储 | `~/.codex/memories/` | `.project/goal-memory/` | 对话历史 + 项目文件 |
| **记忆版本管理** | Git 检查点 | Git 工作区 | 无（原子写入） | 无 |

---

## 关键差异分析

### 1. 记忆粒度

| Agent | 粒度 | 说明 |
|---|---|---|
| Cline | 完整对话 | 保留所有消息，包括工具调用细节 |
| Codex | 结构化摘要 | 从对话中提取关键信息，生成 `raw_memory` 和 `rollout_summary` |
| ProofTask | 结构化事实 | 只保留验收条件、决策、证据，丢弃对话历史 |
| Claude Code | 完整对话 | 保留所有消息，依赖模型长上下文 |

### 2. 记忆生命周期

| 阶段 | Cline | Codex | ProofTask | Claude Code |
|---|---|---|---|---|
| 生成 | 实时保存 | Phase 1 异步提取 | 实时持久化 | 实时保存 |
| 整合 | 无 | Phase 2 全局整合 | 无 | 无 |
| 存储 | VSCode 存储 | Git 工作区 + SQLite | JSON 文件 | 对话历史 |
| 清理 | 无 | 自动清理过期记忆 | Goal 归档 | 无 |
| 读取 | 直接加载 | 注入开发者指令 | prompt 组装 | 直接加载 |

### 3. 记忆注入方式

| Agent | 注入方式 | 说明 |
|---|---|---|
| Cline | 对话上下文 | 作为消息历史的一部分 |
| Codex | 专门的记忆读取路径 | 通过 `read/templates/memories/read_path.md` 注入 |
| ProofTask | prompt 组装 | `build_goal_act_prompt()` 注入结构化事实 |
| Claude Code | 对话上下文 | 作为系统提示的一部分 |

### 4. 恢复策略

| Agent | 恢复策略 | 说明 |
|---|---|---|
| Cline | 从保存的对话历史恢复 | 标记中断点，处理中断的工具调用 |
| Codex | 从 Thread 持久化状态恢复 | 支持 `resumeThread()` |
| ProofTask | 智能推算恢复点 | `_resume_target()` 验证前置条件 |
| Claude Code | 从对话历史恢复 | 依赖完整的对话上下文 |

---

## 设计哲学对比

### Cline：**完整性优先**
- 保留所有信息，不丢失任何上下文
- 依赖 Git 检查点提供可回滚性
- 适合需要精确回溯的场景

### Codex：**结构化记忆**
- 从对话中提取关键信息，生成结构化记忆
- 两阶段管道确保记忆质量和一致性
- 适合长期运行的 agent，需要积累知识

### ProofTask：**事实驱动**
- 只保留可验证的事实（验收条件、证据、决策）
- Worker 是无状态的，通过 prompt 注入回忆
- 适合需要严格验证的自主执行场景

### Claude Code：**简单直接**
- 依赖模型的长上下文能力
- 用户手动维护项目知识（CLAUDE.md）
- 适合简单的编码辅助场景

---

## 可借鉴的设计

### 从 Codex 借鉴
1. **两阶段记忆管道**：提取 + 整合，确保记忆质量
2. **自动脱敏**：处理 secrets 和敏感信息
3. **记忆生命周期管理**：自动生成、整合、清理
4. **Git 版本管理**：记忆的变更历史可追溯

### 从 Cline 借鉴
1. **Git 检查点**：工具执行后自动创建检查点
2. **智能压缩策略**：根据压力程度调整压缩比例
3. **Hook 系统**：允许在压缩前注入自定义上下文

### 从 ProofTask 借鉴
1. **结构化事实传递**：不传对话历史，只传关键事实
2. **智能恢复点推算**：验证前置条件后再恢复
3. **决策日志**：记录关键策略判断，供新 Worker 参考
4. **Worker 交接快照**：完整的上下文快照，支持无缝切换

---

## 未来演进方向

### 短期（可立即实施）
1. **增强 MEMORY.md**：支持更多结构化字段（项目约定、已知陷阱、常用命令）
2. **决策日志优化**：支持按类型筛选决策（修复、影响分析、全局回归）
3. **记忆压缩优化**：在压缩前提取关键事实到 MEMORY.md

### 中期（需要架构调整）
1. **借鉴 Codex 的两阶段管道**：异步提取 + 全局整合
2. **添加记忆清理机制**：自动清理过期的决策日志和交接快照
3. **支持记忆版本管理**：使用 Git 管理记忆的变更历史

### 长期（需要重大重构）
1. **统一记忆层**：将 Goal 记忆、项目知识、对话历史统一管理
2. **记忆智能检索**：根据当前任务自动检索相关记忆
3. **记忆共享**：支持多个 Goal 之间共享记忆
