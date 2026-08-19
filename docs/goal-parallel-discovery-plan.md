# Goal 并行 Discovery 实施计划

## 结论

规划阶段需要看到与目标相关的全局事实，但不应该让一个 Planner 自由扫描整个仓库。
采用受 Supervisor 控制的 fan-out/fan-in 流程：机器先建立仓库地图，多个只读 Discovery
Agent 并行调查不同维度，机器合并并校验证据，Planner 只消费压缩后的 Discovery Manifest。

Goal 的核心抽象仍然是 `Goal -> Task -> Todo`。Discovery 是规划前的证据阶段，不是新的
执行单位，也不引入 Feature。

## 已确认的现有缺陷（必须先修）

### P0：Draft 没有在慢操作前落盘

当前 `harness/goal/draft.py:create_draft()` 先执行测试收集、Intake 和 Planner，最后才保存
`goal-draft.json`。模型请求、测试收集或网络连接一旦卡住，磁盘上没有阶段、心跳、deadline
或恢复点；Intake 调用也没有传入 operation deadline。

第一步必须在任何外部操作前写入 Draft，并在每个阶段开始/结束时原子更新：

```json
{
  "stage": "discovering",
  "stage_started_at": 0,
  "last_heartbeat": 0,
  "stage_deadline": 0,
  "input_hash": "...",
  "discovery_path": "...",
  "last_error": null
}
```

### P1：状态优先级和失败语义不可靠

- 非法 Intake JSON 不能被解释为空问题；必须 `paused/intake_error`，禁止进入 Planner。
- `/goal status` 不能因为存在旧的 terminal Goal 而隐藏活动 Draft。
- `approve_draft()` 不能在 Goal 真正启动前永久消费 Draft。

状态优先级固定为：

```text
活动 Draft > running/paused Goal > terminal Goal
```

成功启动 Goal 后 Draft 才变为 `consumed`；启动前置条件或线程创建失败时恢复为 `ready`，
并保存 `last_error`，允许重新启动。当前工作区的 `.project/goal.json` 是旧的 sandbox
Goal（`done`），没有 `goal-draft.json`；这正是状态选择测试必须覆盖的场景。

## 目标流程

```text
/goal target
  -> persist Draft(stage=preflight)
  -> machine preflight (project root, adapter, target seeds)
  -> intake (only product decisions)
  -> repo map and symbol shards
  -> parallel Discovery wave
  -> evidence merge and gap detection
  -> Planner (one tool-free call)
  -> plan validator
       |-- needs_more_evidence -> targeted Discovery wave -> fresh Planner call
       '-- ready -> focused test writer -> failing baseline -> user review
  -> /goal run -> Task worker -> machine verify -> evaluate -> repair
```

Discovery 和 Draft pipeline 必须在后台运行，`/goal` 命令只创建状态并立即返回。每个阶段
开始、心跳、结束和错误都要原子写入磁盘，因此进程重启后可以从最后一个完整 job 继续。

## Agent 分工

| Agent | 输入 | 产出 |
| --- | --- | --- |
| `goal_discovery_requirement` | Goal Contract、目标文档 | 行为、边界、明确引用的路径 |
| `goal_discovery_architecture` | Repo Map、入口文件 | 入口、依赖、公共接口和状态流 |
| `goal_discovery_implementation` | 相关文件/符号分片 | 可能修改的符号、调用链、scope |
| `goal_discovery_tests` | VerificationContext、测试目录 | 测试命令、真实 selector、覆盖缺口 |
| `goal_discovery_history` | 过去 Goal 的 TestMap/决策 | 相关历史约束和跨 Task 影响 |

Agent 只能读取 Supervisor 发给它的 `read_roots`/`read_paths`，不能修改文件、运行任意
Bash、扫描 `node_modules` 或递归扫描整个仓库。Agent 不允许再创建子 Agent。

并发默认 3 个，可按 provider 限流；每个 job 有独立 `AgentTaskStats`、取消事件和 deadline。
单个 job 失败只标记该 job，不能取消同一 wave 的其他 job。用户暂停/取消时才取消整 wave。

Discovery 的文件过滤必须在机器层完成：默认排除 `.env*`、密钥/证书、凭据目录、二进制、
生成目录和超大文件；报告写盘前还要对路径和输出做敏感信息脱敏。用户明确要求分析敏感
文件时，也只能记录结论和哈希，不能把原文写入 Manifest 或事件流。

## 大文件切分

不能按任意行号切割。Repo Mapper 先生成语言相关的 symbol outline：

- Python 优先使用 `ast`；
- JavaScript/TypeScript 优先使用项目已有的 TypeScript parser，缺失时使用受限的声明索引；
- 记录 imports、exports、函数/类、起止行和文件哈希；
- 按符号和调用关系生成 shard，并设置最大文件数、字节数和依赖深度。

例如 `App.tsx` 应按 `submit/messageQueue`、后端事件、底栏渲染等职责切分，而不是让多个
Agent 重复读取完整文件。

## 证据契约

Agent 返回 JSON，不返回大段自由文本：

```json
{
  "job_id": "implementation-message-queue",
  "base_revision": "git-head-or-snapshot",
  "evidence": [
    {
      "path": "node_tui/src-open/App.tsx",
      "sha256": "...",
      "symbol": "submit",
      "lines": [1020, 1094],
      "claim": "running 状态目前直接拒绝普通消息"
    }
  ],
  "candidate_scope": ["node_tui/src-open/App.tsx"],
  "related_tests": ["node_tui/test/src_open_sections.test.ts"],
  "gaps": ["没有运行中消息排队测试"]
}
```

Supervisor 负责生成稳定的 Evidence ID，并验证路径存在、哈希仍匹配、引用没有越界。
不同 Agent 的冲突结论必须显式记录并触发定向复查，不能静默覆盖。原始报告写入
`.project/goal-memory/<goal-id>/discovery/`，Planner 只收到有大小上限的 Manifest。

## Planner 合同

Planner 每次只做一次无工具调用，但它的输入是完整的 `Goal Contract + Discovery Manifest`，
不是只有目标文本和测试列表。输出允许两种结果：

```json
{"status":"ready","tasks":[...]}
```

或：

```json
{
  "status": "needs_more_evidence",
  "requests": [{"question":"...", "candidate_paths":["..."]}]
}
```

第二种由 Supervisor 自动发起定向 Discovery，不询问用户。每次补查使用新 Planner 会话，
不把所有历史对话重新塞回上下文。规划修订次数是单次规划阶段的调度预算，不是 Goal 总
生命周期限制；Goal 仍可通过持久化状态和新会话长期运行。

每个 Task 必须包含：

```json
{
  "name": "...",
  "behavior": "...",
  "depends_on": [],
  "scope_paths": ["..."],
  "evidence_refs": ["E1", "E2"],
  "acceptance_cases": [{"id":"AC1","given":"...","when":"...","then":"..."}],
  "test_strategy": "...",
  "discovery_revision": 1
}
```

机器校验依赖无环、证据引用存在、scope 在项目根目录内、每个 AC 有测试策略。模型不得
提供验证命令，命令由 VerificationAdapter 生成。

## 验证适配器

抽象 `VerificationAdapter`，至少实现：

```text
discover(context)
normalize_selector(value)
build_command(bindings)
collect(context)
run(command, context)
```

第一批适配器：`PytestAdapter` 和 `NodeTestAdapter`。Task 保存 `project_root`、adapter、
test roots 和文件哈希。Node test 没有 pytest 式 collect-only 时，先采用真实测试文件级
绑定，再由适配器生成 `node --import tsx --test ...` 命令，不把 Node 项目误判成 pytest。

测试绑定必须显式声明 `case -> selector`，禁止“绑定一个测试就自动覆盖全部 AC”。机器只
判定 selector 存在、命令退出码、基线、哈希和证据完整性；测试语义由独立 Evaluator 审查，
但 Evaluator 不能直接改变 Task 状态。

## 状态与恢复

Draft 增加 `stage`、`stage_started_at`、`last_heartbeat`、`stage_deadline`、
`discovery_path`、`plan_revision` 和 `last_error`。推荐阶段：

```text
created -> preflight -> intake -> clarifying -> discovering -> planning -> ready
                                                        |                 |
                                                        +-> failed        +-> consumed
```

`/goal status` 优先显示活动 Draft，其次是 running/paused Goal，最后才显示 terminal Goal。
Draft 只有在 Goal 成功创建后才标记 `consumed`；启动失败保留为可恢复的 `ready`。

## 并发和 UI 约束

- Supervisor 使用受限线程池，不复用旧 teammate 的全局 active 状态；
- provider 通过 semaphore 限制同时请求数；
- Discovery 只发结构化 `goal_discovery_job`、`subagent_*` 事件，经典 CLI 不并发打印富文本；
- `events.emit()` 已有锁，但 Renderer 的 Rich 输出不应由多个 worker 直接写；
- 不调用进程级 `clear_cancel()` 作为单 job 超时手段；单 job 使用自己的取消事件，只有
  用户暂停/取消才广播 Goal 级取消；
- Draft 需要独立 lease，防止两个 `/goal` 命令同时启动两套 Discovery pipeline；
- 每个 job 的完成、超时、取消和证据路径都进入 `discovery-state.json`。

## 实施顺序与验收

1. 先写红测试并实现 Discovery 数据模型、原子存储、Repo Map 和 shard 过滤。
2. 给 Agent executor 增加 `read_roots/read_paths`，并验证越界读取、glob 和 Bash 均被拒绝。
3. 实现 Supervisor 的并发、provider 限流、取消、心跳、结果合并和缺口补查。
4. 抽取 `VerificationAdapter`，加入 Python fixture 和 `node_tui` 风格 Node fixture。
5. 将 Draft 改为后台可恢复 pipeline，接入 Manifest 驱动 Planner 和 plan validator。
6. 增加显式 AC-测试映射、Task scope/evidence/revision 字段和 worker diff 门禁。
7. 接入历史 TestMap/决策，验证新 worker rollover 不丢失 Discovery 和规划证据。
8. 运行 Goal 合同测试、fixture 集成测试、TUI 状态测试和 `python -m evals`。

建议的新增模块边界：

```text
harness/goal/discovery_models.py   # Job/Evidence/Manifest 数据契约
harness/goal/discovery.py          # Repo Map、分片和 Supervisor
harness/goal/discovery_store.py    # job 状态、原始报告和 Manifest 原子存储
harness/verification/adapters.py   # Adapter 协议和项目上下文
harness/verification/pytest_adapter.py
harness/verification/node_adapter.py
```

必须通过的回归场景：

- 多个 Discovery job 并行但最多不超过配置并发数；
- 一个 job 超时不影响其他 job，重启后只重跑未完成 job；
- Planner 缺证据时自动补查，不询问用户；
- `node_tui` Goal 选择 Node adapter 并找到真实测试；
- 非法 selector 或虚假的 AC 覆盖映射不能进入执行；
- 越界读取/写入和越界 diff 会被机器拒绝；
- 旧 terminal Goal 不会隐藏新 Draft；
- 长 Goal 可以跨多个 worker 会话继续，且不受总轮数/总时间限制。
