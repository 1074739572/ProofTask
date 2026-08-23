# ProofTask

**面向大型抽象需求的 Goal 执行系统。**

ProofTask 不把“模型输出了很多内容”当作完成。它先探索仓库、形成证据和任务合同，再让
受限 worker 定向实现，并用机器验证决定每一步能否继续。整个过程可暂停、恢复、审计，
失败也会留下下一步该怎么做。

```text
用户需求
  -> Discovery 证据
  -> Goal 合同与 Task 图
  -> 聚焦测试和失败基线
  -> 定向实现与机器验证
  -> 修复/重规划
  -> 全量回归与交付
```

## 项目重点

ProofTask 围绕四件事设计：

1. **先规划清楚再执行**：把大范围探索放在 Discovery，把架构决定写进 Goal 合同，避免每个 worker 从头理解仓库。
2. **把大目标拆成可交付 Task**：每个 Task 有验收条件、依赖、可读/可写范围、测试策略和证据归属。
3. **让执行过程看得见**：页面展示当前阶段、每个 Agent 的动作、执行前检查、验证结果、修复路线和阻塞原因。
4. **让失败有去处**：系统区分实现问题、测试缺口、范围遗漏、需要重规划和外部阻塞；不再只是报错后停止。

## 运行阶段

| 阶段 | 系统在做什么 | 阶段产物 |
| --- | --- | --- |
| 1. 需求预检 | 确认需求、项目环境、测试入口和必要澄清 | 可执行需求或待回答问题 |
| 2. Discovery | 并行只读探索相关代码、测试、历史和配置 | 证据清单与相关路径 |
| 3. 规划 | 编译 Goal 合同、Task 图、范围、依赖和验收条件 | 已审查的任务合同 |
| 4. 测试准备 | 绑定已有测试或生成聚焦测试并确认失败基线 | selector、AC 映射、测试证据 |
| 5. 执行前检查 | 检查依赖、范围、文件状态和验证前提 | 可执行结论或精确阻塞原因 |
| 6. 定向实现 | 当前 worker 只在 Task 合同允许范围内工作 | 代码变更与执行切片记录 |
| 7. 验证与修复 | 机器验证，必要时补实现、补测试、修正范围或重规划 | 失败分类和下一步决策 |
| 8. 最终交付 | 重验全部 Task 并跑全局回归 | 可审计的交付证据 |

## 为什么不是普通 Agent Loop

| 普通循环式 Agent | ProofTask |
| --- | --- |
| 继续尝试直到模型认为完成 | 验证证据决定是否能推进 |
| 每轮可能重新探索整个仓库 | Discovery 先沉淀证据，执行按合同定向读取 |
| Todo 或模型总结容易被当作完成 | 只有绑定测试、clean check 和最终回归能交付 |
| 失败后重新问同一个模型 | 失败先分类，再选择修复、补测、范围修正或重规划 |
| 长任务靠无限上下文 | Goal/Task/证据落盘，可跨会话恢复 |

## 快速开始

```bash
pip install -r requirements.txt
python main.py
```

在界面中创建一个 Goal：

```text
/goal 为订单列表增加分页、边界测试和回归验证
```

常用命令：

```text
/goal preview
/goal answer <回答>
/goal approve
/goal status
/goal pause
/goal resume
/goal cancel
```

`/goal approve` 在合同通过后启动受控执行。也可以用
`/goal --verify "<command>" -- <需求>` 指定最终全局回归命令。

## 交付标准

一个 Task 必须满足：验收条件有绑定测试、测试真实收集且以 `exit_code = 0` 通过、证据已
保存、clean check 通过。一个 Goal 还必须重跑所有已完成 Task 的绑定测试，并通过全局
回归命令。

模型不能通过删除、跳过或弱化测试来获得“通过”。

## 技术结构

```text
harness/goal/          Goal 草案、规划、执行、修复、持久化
harness/tasks.py       Task 合同、依赖、验证证据
harness/verification/  测试目录、执行适配器、机器证据
config/agents.json     各阶段 Agent 的角色与工具
node_tui/              实时 Goal 页面和执行决策记录
```

## 验证与贡献

Goal 相关改动至少运行：

```bash
python -m pytest -q tests/test_goal_planning_v2.py tests/test_goal_execution_v2.py tests/test_agent_read_scope.py
cd node_tui && npm run typecheck
```

贡献时优先维护 Goal 合同、状态机、恢复、可观察性和测试之间的一致性。新的能力应回答：
它在生命周期哪个阶段发生，留下什么可验证产物，失败时如何恢复或阻塞。

## 文档

- [项目规则与阶段约束](HARNESS.md)
- [Goal 系统总览：范围、重规划与失败路线](docs/goal-system-guide.md)
- [设计原则与机器交付判定](docs/proof-task-design.md)
- [文档索引](docs/README.md)
