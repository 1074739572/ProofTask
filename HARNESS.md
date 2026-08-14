# ProofTask

可验证的自主编码执行器。ProofTask 让 AI 不只会反复改代码，而是能用测试结果证明：这次改动真的可以交付。

复杂需求会被拆成独立的 Task；每个 Task 都有验收标准、真实测试绑定和机器验证证据。

```text
Goal -> Task -> Acceptance Cases -> Test Binding -> Evidence -> Delivery
```

## Commands

- 安装依赖：`pip install -r requirements.txt`
- 启动命令行：`python main.py`
- 运行 Goal 定向测试：`python -m pytest -q tests/test_goal_module.py tests/test_goal_task_contract.py tests/test_goal_clean_scope.py`
- 运行完整可靠性评估：`python -m evals`
- 创建 Goal 草案：`/goal 修复分页接口并补齐边界测试`
- 确认 Goal：`/goal preview`、`/goal answer <回答>`、`/goal approve`、`/goal run`
- 控制 Goal：`/goal status`、`/goal pause`、`/goal resume`、`/goal cancel`

`--verify` 是可选的全局回归覆盖命令；默认由 `HARNESS.md`、测试配置和实际收集结果推断。`/goal approve` 只会写测试并确认失败基线，`/goal run` 才允许业务实现。每个 Task 另有自己绑定的聚焦测试。

## Hard Constraints

- Goal 只使用 `Task`，不再混入旧的 Feature 状态或兼容字段。
- Task 必须有 Given / When / Then 验收条件、依赖关系及真实的测试 selector。
- selector 必须来自 pytest 实际收集的测试目录，模型不能编造文件、测试名或验证命令。
- 缺少合适测试时，`goal_test_writer` 只能为当前 Task 写聚焦回归测试。
- 新生成的测试必须在实现前先失败；基线通过、超时或出错时，Goal 暂停而不是绑定弱测试。
- Todo 是 Agent 的临时工作笔记，不能替代 Task 的验收和验证。
- Task 只有在绑定测试成功、证据已保存且 clean check 通过后才能完成。
- Goal 完成前会强制以 `enforce` 模式执行 clean check，并重跑全部已绑定的 Task 测试。
- 不得删除、跳过或弱化测试来制造通过结果。

## Task Routing

- Goal 的规划和状态机：`harness/goal/`。
- Task 生命周期、验收条件、依赖和证据记录：`harness/tasks.py`。
- 测试收集、执行、重验和证据规范：`harness/verification/`。
- 测试生成代理配置：`config/agents.json` 中的 `goal_test_writer`。
- 设计与迁移说明：`docs/goal-task-verification-plan.md`。

Goal 执行顺序：

```text
PLAN -> TEST CATALOG -> PREPARE TESTS -> SELECT TASK -> ACT -> VERIFY
     -> EVALUATE -> CLEAN CHECK -> IMPACT REVIEW -> next Task
                     |                |
                     v                v
                REPAIR PLAN      cross-Task tests
                     |
                     v
             additive test prep or a fresh ACT worker
     -> FULL VERIFY (all Task bindings + global regression)
```

任务依赖未完成时，后续 Task 不会开始。任何失败都回到同一个 Task 继续修复，不会跳到下一项。

## Definition Of Done

一个 Task 完成，必须同时满足：

1. 验收条件已明确，依赖已满足。
2. 绑定的 selector 能被真实收集。
3. 运行结果为 `exit_code = 0`，且已记录命令、输出摘要、收集数量和代码快照。
4. 严格 clean check 通过；启用 evaluator 时必须明确返回 `passed: true`，失败会进入 Repair Plan，不能放行。

一个 Goal 完成，必须同时满足：

1. 所有 Task 均已完成。
2. 最终阶段重新运行每个 Task 的绑定测试，全部通过。
3. 配置的全局回归命令通过。
4. 每次修复、跨 Task 补测和最终回归失败都保留在 Goal 的决策账本与 TestMap 中；恢复或切换内部 worker 后继续加载。

## Working Model

| 普通循环式 Agent | ProofTask |
| --- | --- |
| 关注下一轮继续尝试 | 关注下一步是否有足够验证证据 |
| 测试命令可由模型临时猜测 | selector 必须来自系统收集的测试目录 |
| Todo 容易被误认为完成 | Todo 不影响 Task 验证状态 |
| 失败可能混进后续任务 | 失败固定回到当前 Task |
| 重启依赖对话上下文 | Goal / Task 图和状态可持久化恢复 |

循环负责推进；验证负责决定能否推进。

## Development

提交任何行为变更前，运行：

```bash
python -m pytest -q tests/test_goal_module.py tests/test_goal_task_contract.py tests/test_goal_clean_scope.py
python -m evals
```

为行为变化补充相应测试。独立的 Feature 工具仍可用于自身工作流，但 Goal 模式不创建或依赖 `.features/` 作为执行状态。
