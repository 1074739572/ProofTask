# ProofTask 项目规则

## 项目目标

ProofTask 的核心不是“让模型多做几轮”，而是把大型、抽象的需求变成可审计、可恢复、
可验证的 Goal 交付过程。

```text
Goal -> 证据 -> 任务合同 -> 实现 -> 验证 -> 交付
```

模型负责理解、提出方案和实现；系统负责保存事实、限制权限、运行验证并裁定能否推进。

## Goal 生命周期

| 阶段 | 重点 | 必须产出 |
| --- | --- | --- |
| 需求与预检 | 识别目标、运行环境、测试入口和真正需要澄清的产品选择 | 可执行请求或明确问题 |
| Discovery | 只读探索相关仓库区域，不让后续 worker 重复全仓搜索 | 证据清单、相关路径、测试目录、风险/冲突 |
| 规划与审查 | 将证据编译为冻结 Goal 合同和 Task 图，并由独立模型审查 | Goal 合同、Task 依赖、验收条件、范围、测试策略 |
| 测试准备 | 复用真实 selector 或为当前 Task 生成聚焦测试 | 测试绑定、失败基线、AC 到 selector 的映射 |
| 执行前检查 | 检查依赖、范围、文件存在性和验证前提 | 可执行/不可执行的明确结论 |
| 定向实现 | worker 只在当前 Task 的允许范围内读取和修改 | 受限代码变更、切片交接记录 |
| 验证与修复 | 先由机器验证，再按证据选择修复、补测、修正范围或重规划 | 验证证据、失败分类、修复决策 |
| 最终交付 | 重跑所有 Task 绑定测试和全局回归 | 完整证据链与最终结果 |

## 不可违反的规则

1. **Discovery 在规划前。** 执行 worker 只能读取当前 Task 的 `read_envelope`、目标源码和绑定测试，不能用大范围探索重新猜项目结构。
2. **合同先于实现。** 每个 Task 必须有行为、Given/When/Then 验收条件、依赖、测试策略和精确范围。
3. **范围是权限边界。** `primary_write` 是直接可改的已有文件；`planned_new` 是允许新建的路径；`conditional_write` 必须有后续证据；`forbidden` 永远不可改。
4. **测试不能编造。** selector 来自系统收集；新测试要在实现前证明缺失行为，不能用弱测试制造通过。
5. **模型自报不算完成。** Task 需要零退出码验证证据和 clean check；Goal 还需要全部 Task 重验和全局回归。
6. **失败不丢失。** 每次失败必须留下原因、证据和下一条路线，不允许静默回到空白 worker。
7. **长任务可延长，但不能盲飞。** worker 达到单次轮数或 token 边界且有进展时，先验证当前结果，再决定是否创建下一切片。
8. **重规划只改未完成部分。** 保留已完成 Task 和冻结 Goal 合同；新的剩余任务图必须重新通过合同校验和独立审查。
9. **敏感边界必须停下。** 工作区外、密钥、部署、不可逆操作、缺失外部环境或 provider 不可用，必须保留阻塞原因，不能自行放权。

## 范围判断

- **过宽**：将整个目录或无关模块放进 `primary_write`；一个 Task 同时决定架构、实现多项独立能力、修改配置并补多层测试；worker 必须自行判断产品边界。
- **过窄**：绑定测试或确定调用链证明需要的源码不在范围内；计划新建文件缺少必要的已有注册/导出点；合同路径已失效。
- **合适**：新 worker 不需要猜架构，能说明改哪些精确文件、为何修改、用哪些测试证明；能独立验收的交付物应拆开。

## 失败路线

| 路线 | 适用情况 |
| --- | --- |
| `implementation_fix` | 验证给出了明确的实现缺口 |
| `test_gap` | 验收行为缺少有效的聚焦测试 |
| `scope_omission` | 未变更的绑定测试证明遗漏了必要源码路径 |
| `replan` | 多次无进展，或任务边界、依赖、测试接缝本身错误 |
| `blocked` | 需要外部授权、环境、凭据或无法安全判断的事实 |

`replan` 不是“重新写测试”。它依据原始 Discovery 证据，在冻结 Goal 合同内重新编译
未完成的 Task，并由独立审查器确认后才替换旧任务。

## 代码地图

- `harness/goal/draft.py`：需求预检、Discovery、规划草案。
- `harness/goal/planner.py`：Goal 合同、Task 合同和独立规划审查。
- `harness/goal/runner.py`：执行状态机、执行前检查、worker、验证、修复和恢复。
- `harness/tasks.py`：Task、依赖、证据和完成状态。
- `harness/goal/repair.py`：修复路线选择。
- `harness/verification/`：测试收集、运行和证据规范。
- `node_tui/src-open/GoalView.tsx`：阶段、Task 合同和执行决策记录。

## 变更要求

改动 Goal 行为时，同时更新对应状态机/恢复逻辑、页面可观察性和回归测试。优先运行：

```bash
python -m pytest -q tests/test_goal_planning_v2.py tests/test_goal_execution_v2.py tests/test_agent_read_scope.py
cd node_tui && npm run typecheck
```

更完整的设计和故障处理见 [docs/goal-system-guide.md](docs/goal-system-guide.md)。
