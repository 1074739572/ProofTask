# Goal 模式下的长短期记忆架构

## 一、短期记忆（Worker 生命周期内）

| 载体 | 内容 | 生命周期 |
|---|---|---|
| **Worker `messages`** | 当前 ACT worker 的完整对话历史 | 单个 worker 存活期间，worker 到达 `worker_round_limit` 后**全部丢弃** |
| **压缩摘要** | `compact_history()` 生成的上下文摘要 | 随 worker 销毁 |

关键设计：Worker 是**一次性消耗品**。到达 round limit 后创建新的 worker，旧的对话历史不传递——只传递结构化事实。

---

## 二、长期记忆（跨 Worker、跨会话持久化）

### 2.1 Goal 主状态 (`.project/goal.json`)

记录 Goal 整体进度和状态机位置，进程重启后靠它恢复。

```json
{
  "id": "goal_1724150400_a3f2",
  "target": "修复分页接口并补齐边界测试",
  "phase": "act",
  "status": "running",
  "current_task_id": "task_1724150500_b7c1",
  "task_ids": ["task_..._b7c1", "task_..._c9d3", "task_..._e1f5"],
  "worker_generation": 3,
  "worker_rollovers": 1,
  "total_llm_rounds": 47,
  "repair_attempts": 2,
  "consecutive_failures": 0,
  "no_progress_count": 0,
  "goal_contract": {
    "scope": "仅修改 api/pagination.py 和 tests/",
    "forbidden": "不得改动数据库 schema"
  },
  "transition_log": [
    {"from": "initialize", "to": "select_task", "reason": "initialize_complete", "attempt": 0},
    {"from": "select_task", "to": "act", "reason": "task_selected", "attempt": 1}
  ]
}
```

**写入时机**：每次状态转换时 `save_goal()` 原子写入。
**读取时机**：`/goal status`、`/goal resume`、进程重启恢复。

### 2.2 决策日志 (`.project/goal-memory/<goal_id>/decisions.jsonl` + `decision-log.md`)

记录关键策略判断，新 Worker 启动时注入最近 12 条。

有 **3 个写入点**：

#### (1) 修复规划决策 (`source="repair_planner"`)

当 Task 验证失败进入 `REPAIR_PLAN` 阶段时，修复规划器的 assumptions 被记录为决策。

```python
# runner.py:1431-1432
if decision.assumptions:
    append_decisions(state, task, list(decision.assumptions), source="repair_planner")
```

示例：*"假设分页 offset 参数未做类型校验，测试失败日志显示 TypeError at line 42"*

#### (2) 跨 Task 影响决策 (`source="test_impact_review"`)

当一个 Task 完成后，系统审查它对其他待办 Task 的影响。如果需要为下游 Task 补充交互测试：

```python
# runner.py:1550-1555
append_decisions(
    state, target,
    [{"decision": "Add cross-Task coverage",
      "basis": decision.reason or f"impact from {completed.id}"}],
    source="test_impact_review",
)
```

示例：*"Task A 修改了 PaginationResult 类，Task B 的排序接口依赖该类，需要补充分页+排序的联合测试"*

#### (3) 全局验证分析 (`source="final_verification_analysis"`)

当最终全局回归测试失败时，系统分析失败原因并决定下一步：

```python
# runner.py:1669-1677
append_decisions(
    state, None,
    [{"decision": f"Full verification analysis: {decision.action}",
      "basis": decision.summary or decision.instructions or ...}],
    source="final_verification_analysis",
)
```

示例：*"全局回归发现 test_sort_with_pagination 失败，根因在 Task A，需要重新打开 Task A 修复"*

**数据格式**：

```jsonl
{"at":1724150600,"source":"repair_planner","task_id":"task_..._b7c1","decisions":[{"decision":"假设 offset 参数未做类型校验","basis":"TypeError at line 42"}]}
{"at":1724150700,"source":"test_impact_review","task_id":"task_..._c9d3","decisions":[{"decision":"Add cross-Task coverage","basis":"Task A 修改了 PaginationResult 类"}]}
{"at":1724150800,"source":"final_verification_analysis","task_id":null,"decisions":[{"decision":"Full verification analysis: reopen_task","basis":"全局回归发现 Task A 破坏了 Task C 的测试"}]}
```

### 2.3 Worker 交接快照 (`.project/goal-memory/<goal_id>/handoff.json`)

Worker 到达 round limit 后，新 Worker 通过它了解上下文。

```json
{
  "goal_id": "goal_1724150400_a3f2",
  "phase": "act",
  "target": "修复分页接口并补齐边界测试",
  "goal_contract": {"scope": "仅修改 api/pagination.py 和 tests/"},
  "task": {
    "id": "task_..._b7c1",
    "subject": "修复分页 offset 类型校验",
    "acceptance_cases": [{"id":"AC1","given":"offset 为字符串","when":"调用 GET /api/items?page=abc","then":"返回 400 而非 500"}],
    "verification_spec": {"selectors":["tests/test_pagination.py::test_offset_type_error"]},
    "last_error": "test_offset_type_error FAILED: expected 400 got 500",
    "repair_history": [{"action":"implementation_fix","summary":"需要在 parse_params 中加 int() 转换"}]
  },
  "decisions": [{"decision":"假设 offset 参数未做类型校验","basis":"TypeError at line 42"}],
  "summary": "上一个 Worker 尝试在 parse_params 加了 try/except，但没处理空字符串情况，测试仍失败。"
}
```

**写入时机**：Worker 开始前 + 结束后（`write_handoff()`）。
**读取时机**：新 Worker 启动时通过 `build_goal_act_prompt()` 注入。

### 2.4 测试绑定表 (`.project/goal-memory/<goal_id>/test-map.json`)

记录哪些测试文件绑定到哪些 Task，防止跨 Task 误删测试。

```json
[
  {
    "binding_id": "task_..._b7c1:r1",
    "revision": 1,
    "task_ids": ["task_..._b7c1"],
    "selectors": ["tests/test_pagination.py::test_offset_type_error", "tests/test_pagination.py::test_offset_empty_string"],
    "test_hashes": {"tests/test_pagination.py": "a1b2c3d4..."},
    "covers": ["AC1", "AC2"],
    "kind": "task",
    "baseline_evidence": {"exit_code": 1, "stdout_tail": "FAILED test_offset_type_error"},
    "created_at": 1724150550
  }
]
```

**写入时机**：测试生成后（`record_test_binding()`）。
**读取时机**：验证/清理/影响审查时（`load_test_map()`）。

### 2.5 Task 完整记录 (`.project/tasks/<task_id>.json`)

每个 Task 的完整生命周期记录。

```json
{
  "id": "task_1724150500_b7c1",
  "subject": "修复分页 offset 类型校验",
  "description": "GET /api/items 的 offset 参数应校验为整数，非法值返回 400",
  "status": "in_progress",
  "acceptance_cases": [
    {"id": "AC1", "given": "offset 为字符串", "when": "调用 GET /api/items?page=abc", "then": "返回 400"},
    {"id": "AC2", "given": "offset 为空字符串", "when": "调用 GET /api/items?page=", "then": "返回 400"}
  ],
  "verification_spec": {
    "adapter": "pytest",
    "command": "pytest tests/test_pagination.py::test_offset_type_error tests/test_pagination.py::test_offset_empty_string -q",
    "selectors": ["tests/test_pagination.py::test_offset_type_error", "tests/test_pagination.py::test_offset_empty_string"],
    "test_hashes": {"tests/test_pagination.py": "a1b2c3d4..."},
    "source": "generated"
  },
  "verification_state": "failing",
  "evidence": [
    {
      "command": "pytest tests/test_pagination.py -q",
      "exit_code": 1,
      "stdout_tail": "FAILED test_offset_type_error - assert 500 == 400",
      "duration_ms": 1230,
      "verified_by": "goal_runner",
      "code_snapshot": "sha256:...",
      "selectors": ["tests/test_pagination.py::test_offset_type_error"],
      "collected_count": 2
    }
  ],
  "evaluation": {
    "passed": false,
    "summary": "只处理了非空字符串，空字符串仍触发 500",
    "route": "implementation_fix"
  },
  "repair_history": [
    {"action": "implementation_fix", "summary": "加 int() 转换并处理 ValueError", "at": 1724150650}
  ],
  "scope_paths": ["api/pagination.py", "tests/test_pagination.py"],
  "start_snapshot": "sha256:e5f6a7b8...",
  "last_error": "test_offset_empty_string FAILED: expected 400 got 500"
}
```

**写入时机**：每个检查点（创建、claim、验证、评估、修复）。
**读取时机**：Runner/验证器/评估器在各阶段读取。

### 2.6 项目级长期知识 (`.memory/MEMORY.md`)

跨 Goal 积累的项目知识，每轮注入 prompt（≤2000 字符）。

```markdown
## 项目约定
- 分页接口统一用 PaginationResult 包装，offset 从 0 开始
- 错误响应格式：{"error": "...", "code": 400}
- 测试文件命名：tests/test_<module>.py

## 已知陷阱
- parse_params 中空字符串不能直接 int()，需要先判断
- SQLAlchemy 的 offset(None) 会抛异常而非返回空
```

**写入时机**：Agent 自动提取或用户手写。
**读取时机**：每轮 prompt 注入（`_goal_worker_context()`）。

### 2.7 已归档 Goal (`.project/goal-history/<goal_id>.json`)

Goal 完成/失败/取消后归档，保留完整快照供审计。

**写入时机**：Goal 终结时（`archive_goal()`）。
**读取时机**：`/goal status` 历史查询。

### 2.8 进程租约 (`.project/goal.lock`)

防止两个进程同时运行同一个 Goal。

```json
{"pid": 12345, "goal_id": "goal_..._a3f2", "token": "af3b2c1d...", "created_at": 1724150400}
```

**写入时机**：Goal 启动时（`acquire_goal_lease()`）。
**读取时机**：进程互斥检查、resume 时判断旧进程是否存活。

---

## 三、Worker 交接机制

当一个 worker 耗尽 round limit 时，`write_handoff()` 生成**交接快照**。新 worker 启动时，`build_goal_act_prompt()` 把这些**结构化事实**注入 prompt——**不注入原始聊天记录**。

---

## 四、会话切换时的记忆加载

### 场景一：Worker 耗尽 Round Limit（进程内换 Worker）

```
_act() 被调用
  │
  ├─ 1. load_task(state.current_task_id)        ← 从磁盘读 Task 完整状态
  │     → 验收条件、evidence、repair_history、last_error
  │
  ├─ 2. _goal_worker_context()                   ← 读 MEMORY.md + project_instructions
  │     → memories (≤2000字符)
  │     → project_instructions (HARNESS.md)
  │
  ├─ 3. build_goal_act_prompt(state, task, ...)  ← 组装 prompt
  │     ├─ Goal 目标和合同
  │     ├─ Task 验收条件 + 绑定测试
  │     ├─ 最近一次 evidence（失败输出）
  │     ├─ recent_decisions(state, limit=12)     ← 读 decisions.jsonl 最近12条
  │     ├─ load_test_map(state) 相关绑定         ← 读 test-map.json
  │     ├─ repair_history（最新修复方向）
  │     ├─ project_instructions
  │     └─ memories (MEMORY.md)
  │
  ├─ 4. write_handoff(state, task, phase="act")  ← 写 handoff.json 快照
  │
  └─ 5. run_agent_task(prompt=..., max_rounds=20) ← 全新 Worker，无旧聊天历史
```

新 Worker 看到的 prompt 长这样：
```
Work only on this Task. Todos are implementation notes, not completion evidence.
Goal: 修复分页接口并补齐边界测试
Task: task_..._b7c1 (修复分页 offset 类型校验)
Required behavior: GET /api/items 的 offset 参数应校验为整数
Task verification state: failing
Bound test command: pytest tests/test_pagination.py::test_offset_type_error -q
Acceptance cases:
  - AC1: Given offset 为字符串; When 调用 GET /api/items?page=abc; Then 返回 400
Last verification error: test_offset_type_error FAILED: expected 500 == 400
Last verification output: FAILED test_offset_type_error - assert 500 == 400
Frozen Goal Contract: {"scope": "仅修改 api/pagination.py 和 tests/"}
Current repair direction: {"action": "implementation_fix", "summary": "需要加 int() 转换"}
Durable decisions from earlier Goal work: [{"decision": "假设 offset 未做类型校验", "basis": "TypeError at line 42"}]
Related TestMap bindings: [{"binding_id": "task_...:r1", "selectors": [...]}]
Project instructions: (HARNESS.md 内容)
Relevant project memory: (MEMORY.md 内容)
```

**不传递的东西**：Worker #1 的聊天历史、工具调用记录、中间思考过程——全部丢弃。

### 场景二：进程重启后 `/goal resume`

```
resume_goal()
  │
  ├─ 1. load_goal()                              ← 读 .project/goal.json
  │     → 恢复 GoalState 全部字段
  │     → phase/status/current_task_id/transition_log 等
  │
  ├─ 2. 检查 lease                                ← 读 goal.lock
  │     → 如果旧进程还活着 → 拒绝 resume
  │     → 如果进程已死 → 清理 lease，继续
  │
  ├─ 3. _discard_legacy_interrupted_final_repair() ← 清理旧进程留下的合成 Task
  │
  ├─ 4. _resume_target(state)                     ← 决定从哪个 phase 恢复
  │     ├─ 初始化未完成？ → INITIALIZE
  │     ├─ Task 文件缺失？ → INITIALIZE
  │     ├─ 未批准执行？ → PREPARE_TESTS
  │     └─ 正常？ → resume_phase（上次暂停的 phase）
  │
  ├─ 5. GoalEngine().transition(state, target)    ← 状态机跳转
  │
  ├─ 6. save_goal(state)                          ← 持久化新状态
  │
  └─ 7. GoalRunner(state=state).start()           ← 启动新线程
        └─ _drive() → _step_once() → 根据 phase 调用对应方法
```

恢复到 ACT 阶段时，走的是跟场景一完全相同的 `_act()` 流程——从磁盘重新加载所有持久化记忆。

---

## 五、记忆加载对照表

| 记忆类型 | 场景一（Worker 轮换） | 场景二（进程重启） |
|---|---|---|
| Goal 状态 | 内存中已有，`save_goal()` 持久化 | `load_goal()` 从磁盘读 |
| Task 状态 | `load_task()` 从磁盘读 | 同左 |
| 决策日志 | `recent_decisions()` 读 jsonl | 同左 |
| 测试绑定 | `load_test_map()` 读 json | 同左 |
| Worker 交接 | `write_handoff()` 写 + prompt 注入 | `load_task()` + prompt 注入 |
| MEMORY.md | `_goal_worker_context()` 读文件 | 同左 |
| 旧 Worker 聊天 | **丢弃** | **不存在**（新进程） |
| 恢复到哪个 phase | 自动（当前 phase） | `_resume_target()` 推算 |

---

## 六、存储总结

| 存储内容 | 文件 | 写入时机 | 谁读取 |
|---|---|---|---|
| Goal 状态机 | `goal.json` | 每次状态转换 | `/goal status`、`/goal resume` |
| 决策日志 | `decisions.jsonl` | 修复规划/影响分析/全局回归分析 | 新 Worker（最近12条） |
| Worker 交接 | `handoff.json` | Worker 开始前 + 结束后 | 新 Worker |
| 测试绑定 | `test-map.json` | 测试生成后 | 验证/清理/影响审查 |
| Task 完整记录 | `tasks/<id>.json` | 每个检查点 | Runner/验证器/评估器 |
| 项目知识 | `MEMORY.md` | Agent 提取或用户编辑 | 每轮 prompt |
| 历史归档 | `goal-history/<id>.json` | Goal 终结时 | `/goal status` 历史查询 |
| 进程锁 | `goal.lock` | Goal 启动时 | 进程互斥检查 |

---

## 七、设计原则

1. **Worker 是无状态消耗品**：聊天历史随 Worker 销毁，不传递给下一个 Worker。
2. **关键事实必须持久化**：决策、验证结果、修复方向以 JSON 写入磁盘。
3. **新 Worker 通过 prompt 注入来"回忆"**：不依赖聊天历史，只依赖结构化事实。
4. **`_resume_target()` 智能推算恢复点**：不盲目跳到上次的 phase，而是验证前置条件是否仍然成立。
5. **原子写入防崩溃**：所有持久化都用 temp file + `os.replace()`，崩溃不会损坏状态文件。
