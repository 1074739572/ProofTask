# Goal 系统总览

更新时间：2026-08-16

本文是 Goal 模式的单一总览入口。它说明系统如何从用户目标走到可验证交付，模型之间和
Task 之间如何传递事实，以及已经遇到的问题为何发生、现在如何处理。其他 Goal 文档保留
为设计细节和历史记录，但阅读与排查应先从本文开始。

## 1. Goal 要解决什么问题

Goal 不是“让模型一直做，直到它说完成”。它是一个持久化的、单 Task 并发度的执行状态机：

```text
用户目标
  -> 澄清与草案
  -> Task 计划
  -> 聚焦测试与基线
  -> 实现一个 Task
  -> 机器验证
  -> 独立评估
  -> 清洁检查与影响审查
  -> 下一个 Task
  -> 全局回归
```

交付的判定顺序是：

```text
机器验证证据 > Task 状态机 > 独立评估 > 模型的文字总结
```

因此 Todo 全部勾选、worker 回复“完成”、或某个无关测试通过，都不能让 Task 自动完成。

## 2. 用户入口与执行边界

常用命令：

```text
/goal <目标>                                  创建草案
/goal preview | answer <文本> | revise <目标>  查看或修改草案
/goal approve                                 确认草案
/goal run                                     允许开始生产代码实现
/goal status | pause | stop | resume | cancel  管理当前 Goal
```

可选参数：

```text
/goal --verify "python -m pytest -q" --worker-rounds 20 --operation-timeout 1800 -- <目标>
```

`/goal stop` 是 `/goal pause` 的安全别名，会保留检查点；`/goal cancel` 才是终止并归档。
`--worker-rounds` 限制一次可丢弃的 worker 会话，`--operation-timeout` 限制一次外部操作；
它们不限制整个 Goal 的总生命周期。worker 到达轮次上限后，会留下持久化状态并进入
rollover，再由验证结果决定是否继续。

草案阶段只读，不会写生产代码。测试已准备好但尚未 `/goal run` 时，Goal 会停在
`user_approval_required`，避免“看了一眼草案”就开始实现。

## 3. 数据模型与持久化

### GoalState

当前 Goal 保存在 `.project/goal.json`，结束后归档到
`.project/goal-history/<goal_id>.json`。状态包含：

- 目标、全局回归命令、冻结的 `goal_contract`；
- 计划、Task ID、当前 Task、依赖与阶段；
- 轮次、尝试、无进展、返修、暂停和错误信息；
- 转换日志，以及全局回归的最终机器证据。

写入使用原子替换；状态机只允许合法转换，并保留最近 100 条转换记录。一个 workspace
同时只能有一个活动 Goal，启动时通过 lease 防止两个 runner 同时写同一状态。

### Task

每个计划项会创建一个独立的 `.tasks/<task_id>.json`，持有：

- 行为说明和验收条件；
- `blockedBy` 依赖；
- `VerificationSpec`、验证状态、机器证据；
- 评估结论、返修历史、开始快照和错误信息。

依赖 Task 只有在真正完成后才会解锁下游 Task。Goal 模式不再依赖旧的 Feature 作为执行
状态，Task 才是唯一的工作与证明单元。

### Task 合同中的 VerificationSpec

`VerificationSpec` 是“如何证明该 Task”的机器合同，包含 adapter、命令、测试文件、
收集到的 selectors、基线、哈希、覆盖的验收条件和 owner。只有 pytest 实际收集到的
selector 才能绑定；模型不能凭空给出路径或命令。

`owners` 大于一个时，代表该测试是跨 Task 的组合覆盖。`impact_context` 则说明为什么
必须有这条组合覆盖，详见第 7 节。

## 4. 状态机与正常路径

```text
INITIALIZE
  -> PREPARE_TESTS（缺少绑定测试）或 SELECT_TASK
  -> CLAIM
  -> ACT
  -> VERIFY
  -> EVALUATE（按配置）
  -> CLEAN_CHECK
  -> IMPACT_REVIEW
  -> SELECT_TASK
  -> ...
  -> FULL_VERIFY
  -> DONE
```

关键阶段：

| 阶段 | 系统动作 | 不能靠什么跳过 |
| --- | --- | --- |
| `INITIALIZE` | 规划、创建 Task、解析依赖 | 模型说计划已完成 |
| `PREPARE_TESTS` | 生成或绑定聚焦测试、验证 selector 与基线 | 手写一个未经收集的测试名 |
| `CLAIM` | 领取当前 Task，记录代码快照 | 前一个 Task 的文字总结 |
| `ACT` | 一个隔离 worker 只实现当前 Task | Todo 完成或 prose completion |
| `VERIFY` | Runner 执行绑定命令并记录证据 | worker 自己声称跑过测试 |
| `EVALUATE` | 只读评估 diff、测试和证据 | 单纯的通过/失败二元结果 |
| `CLEAN_CHECK` | 执行 clean enforce 并完成 Task | 未清理的临时状态或失效证据 |
| `IMPACT_REVIEW` | 检查完成 Task 是否影响后续覆盖 | 假设每个 Task 完全独立 |
| `FULL_VERIFY` | 重跑所有 Task 绑定和全局回归命令 | 只重跑最后一个 Task |

任一非终态都可以进入 `PAUSED`、`CANCELLED` 或 `FAILED`。暂停会记录原阶段为
`resume_phase`，恢复时不会猜测“已经可以开始实现”。

## 5. 模型分工与路由

每个阶段是独立模型会话，不共享聊天历史。当前默认路由如下：

| 角色 | 模型 | 工具与职责 |
| --- | --- | --- |
| `goal_intake` | DeepSeek V4 Pro，max | 只读澄清，避免不必要的用户问题 |
| `goal_planner` | DeepSeek V4 Pro，max | 只读拆 Task、验收条件与依赖 |
| `goal_test_writer` | DeepSeek V4 Flash，max | 只修改新测试文件，返回已收集 selector |
| `goal_worker` | DeepSeek V4 Flash，max | 实现一个已验证 Task |
| `goal_test_impact` | DeepSeek V4 Pro，max | 只读判断是否缺少跨 Task 覆盖 |
| `goal_repair_planner` | DeepSeek V4 Pro，max | 只读决定返修、补测或重新规划 |
| `evaluator` | Xiaomi MiMo V2.5 Pro | 只读独立评估，不改变状态 |

实现 worker 不允许自行运行绑定测试命令，Runner 才是执行验证的唯一主体。若需要 Bash，
worker 只能发一个当前工作目录下的简单命令，不能使用 `cd`、管道、重定向、命令替换或
命令连接符，从而减小权限匹配绕过面。

## 6. 测试、证据与返修

### 测试生成

没有已有绑定测试的 Task 进入 `needs_generation`。测试生成模型只能创建新的聚焦测试文件，
不能改生产代码或既有测试。Runner 会：

1. 在生成前记录测试目录快照和已有 selector；
2. 校验模型返回的 selector 是否被 pytest 新收集；
3. 拒绝复用旧 selector、修改既有测试或生成前已通过的“伪红灯”测试；
4. 对无效尝试恢复测试目录；
5. 将通过校验的测试和基线证据绑定到 Task。

正常情况下，新测试必须在实现前失败，才证明它描述了缺失行为。影响审查触发的补测允许
“后置补测”基线通过，因为它验证的是已完成上游 Task 与后续 Task 的组合行为。

### 机器验证与独立评估

Runner 用 Task 的绑定命令执行验证，并保存退出码、输出尾部、耗时、selector、收集数量
和代码快照。零收集、命令与证据不一致、非零退出码，均不能通过。

评估模型收到行为、验收条件、完整 VerificationSpec、绑定测试源码、机器证据和 git diff。
它可以指出实现缺口、弱测试或范围漂移，返回 `pass`、`implementation_fix`、`test_gap`、
`replan` 或 `blocked`。评估结果进入返修决策，但不直接篡改验证状态。

返修模型只在冻结的 Goal 合同内提出处理方向：继续实现、补测试或重规划。重规划和补测会
回到 `PREPARE_TESTS`，不会假装旧绑定仍能证明新需求。

## 7. 上下文如何交接

模型之间不传递完整聊天记录，而是传递有限、可验证的事实：

```text
planner -> Task 合同 -> 测试生成 -> VerificationSpec
        -> worker -> 机器证据 -> evaluator
```

worker 的 prompt 包含当前 Task、验收条件、绑定测试、最近失败、Goal 合同、相关 TestMap、
持久化决策和项目规则。它不接收其他 Task 的所有历史。

Task 之间另有一条“影响交接”路径：

```text
已完成 Task -> impact review -> 目标 Task 的 impact_context
                                 -> 测试生成 / worker / evaluator
```

impact review 发现共享接口、依赖或组合路径缺少覆盖时，会把上游 Task、影响原因和必需
覆盖写入目标 Task 的 `verification_spec.impact_context`，同时保留旧 selector 并设置组合
测试 owner。测试生成模型据此补交互测试，worker 据此避免破坏上游行为，评估模型据此检查
测试和 diff 是否真正覆盖该路径。

例如：权限引擎把 `external_directory` gate 放到 `read_file` 工具规则之前后，审批记忆
Task 不能只测“记住允许”。它必须测：外部路径触发 ask，批准被记住，下一次请求先通过
外部目录 gate，再应用 `read_file` 规则。

`handoff.json` 只用于审计，不是模型输入。完整说明见
[`goal-context-handoff.md`](./goal-context-handoff.md)。

## 8. 权限与安全

Goal 是非交互执行器，不会在后台自动批准 `ask` 权限。运行时若工具触发人工授权：

```text
工具请求 ask -> hook 标记 permission_pending -> Goal 进入 PAUSED
-> 用户调整权限配置或批准规则 -> /goal resume
```

这保证“需要确认”的工具不会因为 Goal 正在运行而被静默放行。权限等待与普通失败不同，
不会被当成模型返修次数消耗。

沙箱策略将外部目录、写入和 Bash 默认收紧；受保护的 `.features` 与 `.project/goal*`
路径仍被拒绝。规则匹配、外部目录 gate、复合 Bash 检测和 Windows 路径保护都由独立测试
覆盖。

## 9. 可观测性与恢复

Goal 页面展示计划、阶段、当前 Task、验证状态、证据和模型决策流。决策流是从
`subagent_start`、轮次和结束事件归纳出的“当前模型正在做什么”，不显示原始工具调用，
避免将底层调用细节误当作业务进度。

每次阶段转换和每个 Task 证据都会落盘，因此进程重启后可查看状态并恢复。启动时若发现
遗留的运行中 Goal，会将它归类为可恢复状态而不是盲目继续；`/goal resume` 从记录的
`resume_phase` 重新开始。正在运行中的 worker 不会热更新，代码和 prompt 改动从下一次
新会话或重建 prompt 时生效。

## 10. 已遇到的问题与处理

| 问题 | 根因 | 当前处理 |
| --- | --- | --- |
| impact review 输出非 JSON 却被当成 provider 不可达 | 把格式错误和网络/模型错误混在一起 | 对 JSON 做一次受限纠正重试；分别记录 `impact_review_format_error` 与 `provider_unavailable`。 |
| evaluator 输出格式错误导致状态含混 | 评估结果没有区分“无有效结论”和“评估失败” | 进行一次 JSON-only 纠正；无有效结论进入可恢复暂停，不伪造失败或通过。 |
| 上游 Task 影响下游 Task，但只把目标标为补测 | 影响原因只写在审计日志，没有进入后续模型 prompt | 写入 `impact_context`，并注入测试生成、worker 和 evaluator；重绑测试时保留该字段。 |
| `handoff.json` 看似存在却没有实际传递上下文 | 文件只写不读 | 文档明确其审计定位；真正输入来自 Task、TestMap、证据和决策记录。 |
| 测试生成修改旧测试或写出已经通过的测试 | 模型生成结果未被充分约束 | 对目录做快照，拒绝旧 selector/旧文件变更/无效基线，并恢复无效尝试。 |
| 模型调用失败后继续消耗返修预算 | provider 错误被当成普通实现失败 | 识别 provider stop reason，暂停到 `provider_unavailable`，不消耗返修预算。 |
| Goal 需要权限但页面没有可用审批入口 | 非交互 runner 不能安全地弹出普通审批 | 停在 `permission_wait`；用户先调整配置或持久规则，再 `/goal resume`。 |
| 同一 Goal 被多个 runner 同时写入 | 状态文件是共享资源 | 启动 lease，拒绝第二个活动 runner。 |
| 最后一个 Task 通过但早期 Task 回归 | 没有全量回归门槛 | `FULL_VERIFY` 重跑全部 Task 绑定，再执行 Goal 级回归。 |
| 页面只有静态任务图，运行中难以判断模型进度 | 子模型生命周期没有转换成用户可读状态 | 展示模型决策流，并在规划、执行、评估阶段切换当前角色与最后总结。 |

## 11. 排查顺序

遇到 Goal 异常时，按以下顺序看：

1. `/goal status`：当前阶段、`stop_reason`、`last_error`、当前 Task。
2. `.project/goal.json`：转换日志、`resume_phase`、最终回归状态。
3. `.tasks/<task_id>.json`：验收条件、`verification_spec`、证据、评估、返修历史。
4. `.project/goal-memory/<goal_id>/`：TestMap 和决策日志，尤其是 `impact_context` 的来源。
5. 权限审计：确认是否为 `permission_wait`，而不是模型或测试失败。
6. 绑定命令与原始测试输出：先确认收集数量、退出码和命令一致性，再判断代码实现。

不要先相信模型总结，也不要直接编辑 Goal 状态文件跳过阶段；这会破坏可恢复性和证据链。

## 12. 相关代码与回归测试

| 范围 | 主要位置 |
| --- | --- |
| 命令、状态、转换、执行 | `harness/goal/commands.py`、`models.py`、`engine.py`、`runner.py` |
| 草案、规划、返修、影响审查 | `harness/goal/draft.py`、`planner.py`、`repair.py`、`impact.py` |
| 持久化与记忆 | `harness/goal/store.py`、`memory.py`、`harness/tasks.py` |
| 验证与评估 | `harness/verification/`、`harness/evaluation/` |
| 权限停止点 | `harness/hooks.py`、`harness/permissions/` |
| 主要回归测试 | `tests/test_goal_task_contract.py`、`tests/test_goal_impact.py`、`tests/test_evaluation_hardening.py` |

详细专题：[`goal-context-handoff.md`](./goal-context-handoff.md)、
[`goal-task-verification-plan.md`](./goal-task-verification-plan.md)、
[`proof-task-design.md`](./proof-task-design.md)。
