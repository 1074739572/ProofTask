# ProofTask

> 可验证的自主编码执行器。让 AI 从“持续尝试”走到“带着测试证据交付”。

ProofTask 是一个面向真实代码仓库的 Agent Harness。它将复杂需求拆成
可交付的 Task，并让每个 Task 都绑定验收标准、真实测试选择器和机器验证
证据。

```text
模型说完成了          不算完成
Todo 全部完成         不算完成
绑定测试实际通过       Task 才算完成
```

## 它解决什么问题

自主编码 Agent 已经可以连续修改代码，但复杂任务常见的失败不是“不工作”，
而是“看起来做了很多，却没有证明交付正确”。例如：

- 模型编造了一个测试路径，命令看似安全却没有验证任何行为；
- Todo 全部打勾，真正的用户需求仍未满足；
- 多步骤需求之间没有明确依赖，失败的任务被后续工作掩盖；
- 重启后只剩对话上下文，不知道哪个任务、哪次验证、哪个错误需要恢复。

ProofTask 把执行过程收敛为可审计的闭环：

```text
Goal -> Task -> Acceptance Cases -> Test Binding -> Evidence -> Delivery
```

## 核心设计

### 1. Goal 拆成独立 Task

规划器将复杂 Goal 拆解为多个 Task。每个 Task 都包含：

- 明确的行为描述；
- Given / When / Then 验收标准；
- 对前置 Task 的依赖；
- 对应的测试选择器和验证状态；
- 验证证据、错误记录和可选评估结果。

依赖任务没有完成时，后续 Task 不会开始。

### 2. 测试必须真实存在

系统先运行 pytest 收集当前仓库实际存在的测试节点。规划模型只能从这份
目录中选择 selector，不能自行发明文件路径、测试名或验证命令。

如果当前没有能证明某个 Task 的测试，它会进入 `needs_generation`：

1. 测试生成阶段只补充聚焦测试；
2. 系统重新收集 pytest selector；
3. 只绑定真实可收集的 selector；
4. 执行基线测试并记录结果；
5. 再进入实现阶段。

### 3. 机器证据决定完成

每次 Task 验证都会保存：

- 实际执行的命令；
- 退出码；
- 测试 selector 和收集数量；
- 输出摘要；
- 验证时的代码快照。

只有带有 `exit_code = 0` 证据的 Task 才能完成。失败会回到同一个 Task
继续修复，不会跳到下一个任务。

### 4. Todo 不是 Task

Todo 是 Agent 的临时工作笔记，例如“阅读模块”“修改接口”“运行局部测试”。
Task 是用户可验收的交付单元。Todo 的完成不会修改 Task 的验证状态。

这条边界让“过程很忙”与“结果正确”不再混为一谈。

### 5. 评估器只提供第二意见

可选 evaluator 会结合验收标准、代码 diff 和机器证据检查范围偏离、覆盖
不足等问题。它是建议者，不是裁判：不能把缺失或失败的机器验证改判为
通过。

## 执行流程

```text
Goal
  -> Planner: Task、验收标准、依赖
  -> Test Catalog: 收集真实 pytest selector
  -> PREPARE_TESTS: 缺少测试时生成、收集、绑定并做基线
  -> ACT: 一次只处理一个 Task
  -> VERIFY: 运行该 Task 的绑定测试并保存证据
  -> EVALUATE: 可选独立检查
  -> COMPLETE: Task 完成，解锁依赖 Task
  -> FULL VERIFY: 所有 Task 完成后运行全局回归
```

## 快速开始

安装依赖：

```bash
pip install -r requirements.txt
```

启动命令行：

```bash
python main.py
```

启动一个自主 Goal：

```text
/goal --verify "python -m pytest -q" -- 修复分页接口并补齐边界测试
```

控制执行：

```text
/goal status
/goal pause
/goal resume
/goal cancel
```

`--verify` 是所有 Task 完成后的全局回归命令。每个 Task 自己运行的是
Test Catalog 绑定的聚焦测试。

## 与普通循环式 Agent 的区别

| 普通循环式 Agent | ProofTask |
| --- | --- |
| 关注下一轮继续尝试 | 关注下一步是否有足够验证证据 |
| 测试命令可能由模型临时猜测 | selector 必须来自系统收集的测试目录 |
| Todo 容易被误认为交付完成 | Todo 不影响 Task 的完成状态 |
| 失败可能混入后续任务 | 失败固定回到当前 Task |
| 计划、执行、验证分散 | Task 记录同时保存契约、测试和证据 |
| 恢复依赖对话上下文 | Goal / Task 图和状态可持久化恢复 |

ProofTask 不反对循环。循环负责推进，验证负责决定“能不能推进”。

## 安全与边界

- 验证命令经过策略和权限控制；
- 支持超时、暂停、取消和错误记录；
- 不接受模型编造的测试路径或 selector；
- 不允许无证据的 Task 完成；
- 不让 evaluator 覆盖机器测试结果；
- 旧的 Feature 混合 Goal 状态会明确拒绝，不静默迁移到不可靠的模型。

独立的 Feature 工具仍可用于其自身工作流，但 Goal 模式不创建 Feature，
也不依赖 `.features/` 作为执行状态。

## 当前验证

```text
python -m pytest -q tests/test_goal_module.py tests/test_goal_task_contract.py tests/test_goal_clean_scope.py
8 passed

python -m evals
81 passed, 0 failed, 2 skipped
```

详细设计和迁移记录见 `docs/goal-task-verification-plan.md`。

## 适合谁

- 想让 Agent 处理复杂改造，但不接受“它说改好了”；
- 需要把需求拆成有依赖的可交付任务；
- 希望测试文件、测试 selector 和 Task 有明确对应关系；
- 需要可暂停、可恢复、可审计的自主执行过程；
- 正在给团队或开源项目引入 AI 编码工作流。

## 项目定位

ProofTask 不是另一个只会延长上下文的 Agent Loop。

它要把“目标 -> 任务 -> 测试 -> 证据 -> 交付”连成闭环，让复杂任务中的
每一次推进都值得相信。

## 开发与贡献

提交代码前建议运行：

```bash
python -m pytest -q tests/test_goal_module.py tests/test_goal_task_contract.py tests/test_goal_clean_scope.py
python -m evals
```

请为行为变化补充对应测试，不要通过删除、跳过或弱化测试来制造通过结果。
