# 文档索引

这里记录 ProofTask 的设计决策、可靠性改进和实际问题。建议先看项目首页，再按问题选择专题文档。

## 从这里开始

| 想了解什么 | 文档 |
| --- | --- |
| 项目是什么、如何运行 | [`README.md`](../README.md) |
| Agent 应该如何读取项目规则 | [`HARNESS.md`](../HARNESS.md) |
| 为什么要让机器判定交付 | [`proof-task-design.md`](./proof-task-design.md) |
| Goal / Task / 测试验证如何落地 | [`goal-task-verification-plan.md`](./goal-task-verification-plan.md) |

## ProofTask 设计

- [`proof-task-design.md`](./proof-task-design.md)：问题、原则、职责边界和当前实现的完整说明。
- [`goal-task-verification-plan.md`](./goal-task-verification-plan.md)：Goal 状态、Task 契约、测试绑定和完成门槛。
- [`goal-mode-mvp-spec.md`](./goal-mode-mvp-spec.md)：早期 MVP 方案，仅用于了解演进过程；当前实现以 Goal / Task 文档为准。
- [`harness-reliability-plan.md`](./harness-reliability-plan.md)：从评估、状态、验证到自主循环的可靠性建设记录。

## 问题与修复

[`bugs/`](./bugs/) 按“现象 -> 根因 -> 已改动 -> 仍需观察”记录真实问题。重点主题包括：

- 上下文压缩、缓存分层和跨会话恢复；
- Todo 漂移、工具空转和目标偏移；
- 最终回答不可见、权限中断和工具超时；
- Goal 清理检查、Task 依赖和验证状态。

问题总览见 [`bugs/README.md`](./bugs/README.md)。

## 评估与验证

- [`evals.md`](./evals.md)：本地可靠性评估入口和结果说明。
- `evals/`：权限、模式、清理、验证、Goal 契约和机制回归。
- `tests/`：代码行为测试，尤其是 `tests/test_goal_task_contract.py`。

## 其他专题

- [`rag.md`](./rag.md)：本地 RAG 的实现和使用。
- [`rag-evolution.md`](./rag-evolution.md)：从纯文本到 PDF / 表格知识的演进。
- [`rag-evaluation.md`](./rag-evaluation.md)：检索、解析和回答质量的分层评估。
- [`tools.md`](./tools.md)：工具注册、调用和权限边界。
- [`project-instructions.md`](./project-instructions.md)：项目说明文件的加载规则。
- [`teammate-lifecycle.md`](./teammate-lifecycle.md)：子 Agent 的生命周期和隔离。

## 文档约定

设计文档说明“为什么这样设计”；实现文档说明“现在如何工作”；Bug 文档记录“实际遇到了什么”。状态只使用：`已实现`、`部分缓解`、`仍需观察`，避免把计划写成已交付结果。
