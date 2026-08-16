# Goal 上下文交接

> Goal 的完整机制、问题处理和排查入口见 [`goal-system-guide.md`](./goal-system-guide.md)。本文只展开上下文交接。

更新时间：2026-08-16

## 原则

Goal 模式不会让规划、测试生成、实现、影响审查和评估模型共享一段聊天历史。
它们都是独立会话，只通过落盘、可检查的 Goal 和 Task 事实协作。

这是有意的设计。把之前所有对话完整塞给后续 Task，会带入旧猜测、无关工具输出，
并让 token 成本无限增长。一个信息只有写入 Task 合同、测试绑定、验证证据、TestMap
或决策记录后，后续模型才能把它当作事实使用。

`handoff.json` 是审计记录，记录 worker 的交接情况，但不是模型输入，不能把它当作
模型之间的消息通道。

## 两条交接路径

这两条路径解决不同问题。

### 模型阶段交接

```text
planner -> Task contract -> test writer -> bound VerificationSpec
        -> worker -> machine evidence -> evaluator
```

1. 规划模型创建持久化 Task，写入行为、验收条件和依赖。
2. Task 缺少测试时，测试生成模型读取 Task 合同并创建聚焦测试。Runner 校验
   selector 后，将结果写入 `VerificationSpec`。
3. 实现 worker 只读取当前活动 Task 的合同：行为、验收条件、测试绑定和失败输出、
   Goal 合同、相关 TestMap、持久化决策和项目规则。
4. 机器执行绑定命令并保存证据。评估模型读取 Task 合同、验证规格、测试源码、证据
   和实际 diff，返回建议性结论；状态机才拥有最终完成权。

模型的自然语言回复不会自动复制给下一个模型。一段“我完成了”的总结也不是完成证据。

### 跨 Task 影响交接

```text
completed Task -> impact review -> target Task impact_context
                               -> test writer / worker / evaluator
```

每个 Task 完成后，`goal_test_impact` 会将它与待执行 Task 和 TestMap 对比。只有
发现共享接口、依赖或交互路径没有被覆盖时，它才返回 `add_tests`。

对于受影响的目标 Task，Runner 随后会：

1. 请求增量测试准备，将旧 selector 保留为 `previous_selectors`。
2. 将两个 Task 记录为组合测试的验证 owner。
3. 写入结构化的 `verification_spec.impact_context`：

```json
{
  "source_task_id": "task_upstream",
  "source_task_subject": "共享权限引擎",
  "reason": "external_directory gate 现在先于工具规则执行",
  "required_coverage": "在实现前补充上游 Task 与目标 Task 的聚焦交互覆盖；不能只重复 Task 本地测试。"
}
```

4. 将同一结论追加到 Goal 决策日志，便于审计。

上下文只保留最近 8 条，并按上游 Task ID 去重。测试生成模型用新绑定替换其他验证规格
时，系统会保留这段上下文。

## 每个模型收到什么

| 模型 | 跨 Task 输入 | 必须做什么 |
| --- | --- | --- |
| 测试生成模型 | `impact_context`、Task 行为、验收条件 | 添加交互测试，不能只重复本地测试。 |
| 实现 worker | `impact_context`、TestMap、决策、Task 合同 | 实现当前 Task，且不能破坏指定的上游行为。 |
| 评估模型 | `impact_context`、绑定测试源码、证据、diff | 必需交互覆盖缺失时，不能判定通过。 |

评估模型的硬规则很重要：无关的本地测试通过，不能证明跨 Task 的交互仍然正确。

## 示例：沙箱权限

假设上游 Task 改动了权限引擎，让外部目录检查先于 `read_file` 工具规则执行；后续
Task 则负责审批与记忆流程。

只测本地审批逻辑并不够，必须覆盖完整交互路径：

```text
external file -> external_directory asks -> approval is remembered
              -> next request passes that gate -> read_file rule applies
```

影响上下文把这个缺口交给测试生成模型，要求实现 worker 不要绕过新 gate，并让评估模型
检查是否有整条路径的证据。

## 边界

- 下游 Task 不会自动收到所有上游 Task 历史。
- impact review 返回 `none` 时，不会产生额外上下文。
- `impact_context` 只说明为什么必须补覆盖；selector 和机器验证证据才是测试存在且
  通过的证明。
- 已经运行中的 worker 不会热更新。后续阶段重新构建 prompt，或新的 Goal 运行时，才会
  使用新的交接逻辑。
