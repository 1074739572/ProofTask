# Goal 持久化文件参考

本文是面向维护者的 Goal 持久化索引。它以当前正在运行的
`goal_1787843591_c0cc` 为例，说明每类 JSON 文件保存什么、谁写入、谁读取。

当前例子的执行 Task 是 `task_1787882918_25f678169852`：
`修复 LogView 空文本与孤儿文本渲染阻塞`。Goal 有 15 个活跃 Task，当前处于
`act/running`。示例中的长输出、哈希和时间戳均省略，只保留识别和理解结构所需字段。

不要手工编辑这些运行时文件来推进状态或绕过验证。应通过 `/goal` 命令和状态机写入；
文件用于恢复、审计和诊断。

## 文件总览

| 文件或目录 | 逻辑角色 | 当前例子 |
| --- | --- | --- |
| `.project/goal-draft.json` | 规划草稿和候选计划检查点 | `goal_draft_1787841255_4a08`，已 `consumed` |
| `.project/goal.json` | 当前 Goal 的唯一状态机快照 | `goal_1787843591_c0cc`，`act/running` |
| `.tasks/<task_id>.json` | 一个 Task 的唯一执行合同和验证历史 | 当前 LogView Task |
| `.project/goal-memory/<goal_id>/handoff.json` | 当前 Task 的紧凑交接和失败诊断 | 当前 LogView Task 的第 13 次 repair 后状态 |
| `.project/goal-memory/<goal_id>/test-map.json` | Task 到测试证据的绑定表 | LogView 的 4 个 AC selector |
| `.project/goal-memory/<draft_id>/discovery/manifest.json` | Discovery 的汇总证据索引 | 54 个仓库路径、40 条证据、5 个 job |
| `discovery/jobs/<job_id>.json` | 单个 Discovery job 的执行状态 | `implementation-1.json` 已完成 |
| `discovery/reports/<job_id>.json` | 单个 Discovery job 的结构化发现 | `implementation-1.json` 的实现范围报告 |
| `.project/goal-history/<goal_id>.json` | 已结束 Goal 的只读归档 | 例如历史 Goal 的终态快照 |
| `.tasks/archive/<task_id>.json` | 已归档 Task 的只读副本 | 已取消或完成的历史 Task |
| `.project/goal.lock` | 运行中 Goal 的短期进程租约 | 当前 Goal 的 PID、token 和创建时间 |

`decisions.jsonl` 和 `decision-log.md` 也属于持久化知识，但前者是 JSON Lines，
后者是 Markdown，并非一个 JSON 文件；它们在文末单列说明。

## 1. Draft：`goal-draft.json`

**写入者**：`harness/goal/draft.py`。
**读取者**：`/goal preview`、`/goal answer`、`/goal approve`、`/goal resume`。

它保存用户需求尚未变成执行 Goal 前的一切：澄清问答、验证命令、Discovery 路径、
规划候选、独立 review 和恢复位置。当前例子的 Draft 已被消费，说明它已成功变成
`goal_1787843591_c0cc`；它仍保留用于审计。

```json
{
  "id": "goal_draft_1787841255_4a08",
  "status": "consumed",
  "stage": "consumed",
  "verification": "npm test",
  "planning_review": {"approved": true},
  "planning_candidate": {
    "goal_contract": {"summary": "..."},
    "tasks": [{"name": "修复 LogView 空文本与孤儿文本渲染阻塞"}]
  },
  "planning_candidate_meta": {
    "input_hash": "...",
    "catalog_fingerprint": "...",
    "manifest_fingerprint": "...",
    "planner_output_hash": "..."
  }
}
```

`planning_candidate` 是“planner 已生成且机器校验通过、但 reviewer 尚未完成”的
检查点。若 reviewer 空响应或超时，resume 会验证四个 fingerprint 后直接 review
该候选，不会重跑完整 planner。

## 2. Goal 主状态：`goal.json`

**写入者**：`harness/goal/store.py:save_goal()`。
**读取者**：Goal runner、`/goal status`、`/goal resume`、TUI。

这是当前执行的权威状态，而不是聊天记录。它保存状态机位置、冻结合同、Task 图、
执行 trace、验证和暂停原因。

```json
{
  "id": "goal_1787843591_c0cc",
  "phase": "act",
  "status": "running",
  "draft_id": "goal_draft_1787841255_4a08",
  "current_task_id": "task_1787882918_25f678169852",
  "task_ids": ["...共 15 个活跃 Task..."],
  "goal_contract": {"language": "zh-CN", "constraints": ["..."]},
  "execution_trace": [
    {"event": "implementation_slice", "task_id": "task_1787882918_25f678169852", "route": "..."}
  ],
  "final_verification": null
}
```

`execution_trace` 是最近的机器过程事实，例如一次 worker slice、验证失败、证据拒绝的
replan 或 repair threshold。它有长度上限，不代替完整聊天历史。

## 3. Task 合同：`.tasks/task_1787882918_25f678169852.json`

**写入者**：`harness/tasks.py` 和 Goal runner。
**读取者**：执行前检查、worker prompt、验证器、repair planner。

每个 Task 一份，是“当前 Task 能做什么、如何证明完成”的权威来源。当前文件记录：

```json
{
  "id": "task_1787882918_25f678169852",
  "status": "in_progress",
  "subject": "修复 LogView 空文本与孤儿文本渲染阻塞",
  "primary_write": ["src-open/App.tsx"],
  "read_envelope": ["src-open/interaction.ts"],
  "acceptance_cases": ["AC1", "AC2", "AC3", "AC4"],
  "verification_spec": {
    "command": "...bun.exe test test/log-view-empty-text.test.tsx",
    "selectors": ["...AC1", "...AC2", "...AC3", "...AC4"],
    "baseline_result": "failing"
  },
  "repair_history": ["...13 次局部 repair 决策..."],
  "last_error": "verification failed with exit code 1"
}
```

重点字段：`primary_write/planned_new/conditional_write` 是写权限；`read_envelope`
是阅读权限；`acceptance_cases` 是不能删除的验收语义；`verification_spec` 是唯一可用
的 Task 证明；`repair_history` 保存每次 repair 的评价、方向和审计摘要。

## 4. Worker 交接：`handoff.json`

**写入者**：`harness/goal/memory.py:write_handoff()`，由 act、verify、repair 等阶段调用。
**读取者**：新 worker 和 repair planner。

它是紧凑的、Task 局部的交接快照，不是完整日志。当前 Task 的 handoff 中有：

```json
{
  "goal_id": "goal_1787843591_c0cc",
  "phase": "act",
  "task": {"id": "task_1787882918_25f678169852", "subject": "修复 LogView..."},
  "execution": {
    "goal_id": "goal_1787843591_c0cc",
    "task_id": "task_1787882918_25f678169852",
    "attempt_id": "task_1787882918_25f678169852:...",
    "worker_summary": "...",
    "write_paths": ["src-open/App.tsx"],
    "tool_errors": ["..."]
  },
  "failure": {
    "goal_id": "goal_1787843591_c0cc",
    "task_id": "task_1787882918_25f678169852",
    "classification": "implementation_blocker",
    "verification": {"output_tail": "Orphan text error ..."},
    "next_action": "..."
  }
}
```

`execution` 记录 worker 做过的读取、写入和工具事实；`failure` 记录验证失败的分类、
输出尾部、最近尝试和下一步。二者都带 `goal_id/task_id/attempt_id`。repair 只接受与
当前 Goal 和当前 Task 相同的段落；切换 Task 时会清空它们，防止旧失败串到新 Task。

## 5. 测试绑定：`test-map.json`

**写入者**：`harness/goal/memory.py:record_test_binding()`。
**读取者**：验证、影响审查、最终全量回归。

它是一个数组，每条记录将 Task、验收条件和不可变测试证据绑定。当前 Goal 有 13 条
绑定；当前 LogView Task 的第 13 条为：

```json
{
  "binding_id": "task_1787882918_25f678169852:r13",
  "task_ids": ["task_1787882918_25f678169852"],
  "selectors": ["...AC1", "...AC2", "...AC3", "...AC4"],
  "covers": ["AC1", "AC2", "AC3", "AC4"],
  "test_hashes": {"test/log-view-empty-text.test.tsx": "5f8a...10c4"},
  "baseline_evidence": {"exit_code": 1, "verified_by": "goal_test_baseline"}
}
```

它回答“哪组 selector 证明哪个 Task 的哪个 AC，以及测试文件是否在之后被改掉”。
测试 hash 改变或 selector 收集数量不一致时，旧成功不能直接当作完成证明。

## 6. Discovery 汇总：`discovery/manifest.json`

**写入者**：`harness/goal/discovery_store.py:save_manifest()`。
**读取者**：planner、execution replan。

这是规划证据的目录，而不是模型自由文本。当前 Draft 的 manifest 有 `revision: 1`、
54 个仓库路径、40 条证据和 5 个 Discovery job。第一条证据指向
`src-open/App.tsx`，说明 `contextHeader()` 和 `headerText()` 已定义但未被渲染树使用。

```json
{
  "revision": 1,
  "repo_files": ["src-open/App.tsx", "..."],
  "evidence": [
    {"id": "E1", "path": "src-open/App.tsx", "claim": "...", "lines": [332, 346]}
  ],
  "jobs": [{"id": "implementation-1", "status": "done"}]
}
```

replan 只能基于这类证据和冻结 Goal 合同说明新的路径/Task 边界；没有 manifest 时
系统应该暂停，而不是凭模型记忆改计划。

## 7. Discovery job：`discovery/jobs/<job_id>.json`

**写入者**：`harness/goal/discovery_store.py:save_job_state()`。
**读取者**：Discovery 恢复和状态页。

每个 job 是一个可恢复的探索单元。当前 Draft 的
`discovery/jobs/implementation-1.json` 表示实现探索已完成，并链接对应 report：

```json
{
  "id": "implementation-1",
  "status": "done",
  "started_at": 1787842154.8577833,
  "report_path": ".project/goal-memory/goal_draft_1787841255_4a08/discovery/reports/implementation-1.json"
}
```

它用于判断 resume 应重跑哪个失败/缺失 job，而不是无差别重跑整轮 Discovery。

## 8. Discovery report：`discovery/reports/<job_id>.json`

**写入者**：`harness/goal/discovery_store.py:save_report()`。
**读取者**：manifest 汇总、planner 的证据视图。

同一当前 Draft 有 `implementation-1.json`、`architecture-1.json`、`tests-1.json`
等 report。每份保存对应角色的结构化发现：已读路径、候选实现点、测试命令/selector、
claim 与引用。它不直接授予写权限；只有被汇总和验证后的 manifest 证据才能影响合同。

```json
{
  "job_id": "implementation-1",
  "report": {"claims": ["App.tsx 的渲染入口..."], "citations": ["E1", "E24"]}
}
```

## 9. 已结束 Goal：`goal-history/<goal_id>.json`

**写入者**：Goal 到达 `done/failed/cancelled` 后由 store 归档。
**读取者**：历史查询和审计。

形状与 `goal.json` 相同，但它不是当前运行状态，也不会被当前 worker 当作 prompt。
例如当前目录中的 `goal_1787728180_5206.json` 记录一个过去 Goal 的终态快照；当前
`goal_1787843591_c0cc` 尚未结束，所以不应有它的历史归档作为执行依据。

## 10. 已归档 Task：`.tasks/archive/<task_id>.json`

**写入者**：Task 被归档、替换或清理时。
**读取者**：审计和历史排查。

文件形状与普通 Task 相同，但不能重新成为当前 Task 图的一部分。若 replan 替换 Task，
旧 Task 的合同和失败历史应保留在原 Task/归档记录中，新 Task 另有 ID；不能把两者混作
同一份完成证据。

## 11. 进程租约：`goal.lock`

**写入者**：`harness/goal/store.py:acquire_goal_lease()`。
**读取者**：启动、resume 和并发保护。

它是 JSON，但不是业务知识。当前内容的结构是：

```json
{
  "pid": 38612,
  "goal_id": "goal_1787843591_c0cc",
  "token": "...",
  "created_at": 1787889104.7213523
}
```

它只防止两个 runner 同时操作同一个 Goal。runner 正常结束时释放；异常残留时系统先检查
PID 是否仍存活，不能把它当作普通 Goal 状态手工删除。

## 决策记录：`decisions.jsonl` 与 `decision-log.md`

它们不是 JSON 文件，但保存最容易丢失的“为什么”。当前 LogView Task 的记录包括：

```json
{"source":"repair_planner","task_id":"task_1787882918_25f678169852","decisions":[
  {"decision":"继续采用 implementation_fix 而非 replan","basis":"失败是 App.tsx 的具体渲染阻塞，合同和范围未失配"},
  {"decision":"过滤空白时保留非空字符串原内容","basis":"AC1 要丢弃纯空白，AC4 要保留日志、时间和状态前缀"}
]}
```

`decisions.jsonl` 是机器读取的追加记录；`decision-log.md` 是同一内容的人类可读镜像。
新 worker 只接收当前 Task 的近期决策和稳定的 Goal 级决策，不接收其他 Task 的局部死路。

## 不属于本参考范围的 JSON

`.project/active_session.json`、`state.json`、`permissions.*.json`、`usage/`、TUI 历史和
其他产品/工具 JSON 也会持久化，但不属于 Goal 的合同、证据或 worker 交接协议。本参考
不把它们混入 Goal 知识，以免错误地把 UI 会话或权限审计当作执行依据。
