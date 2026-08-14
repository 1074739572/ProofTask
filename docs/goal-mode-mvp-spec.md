> 历史设计说明：本文记录早期 `/goal` MVP 的 Feature 方案，保留用于理解项目演进。当前实现已统一为 Goal / Task 模型；请以 [`proof-task-design.md`](./proof-task-design.md) 和 [`goal-task-verification-plan.md`](./goal-task-verification-plan.md) 为准。

# `/goal` 自主执行模式 MVP 技术规格

> 状态：待实现（供下一会话直接执行）  
> 版本：MVP v1  
> 日期：2026-08-05  
> 依赖：L0 评测、L1 `HARNESS.md`、L2 feature 状态机、L3 验证门控、L4 清洁检查、L5 独立 evaluator（已具备）  
> 总计划：[`harness-reliability-plan.md`](./harness-reliability-plan.md)

---

## 1. 摘要

`/goal` 是一个**显式、持久化、可停止的自主执行状态机**。用户只提交一次目标，Harness 自动编排现有能力：建立 feature、一次只选择一个 feature（WIP=1）、调用 agent 完成修改、执行机器验证、运行清洁检查，直到 feature 通过或触发硬停止条件。

`/goal` 不是“无限重复调用 `agent_loop` 直到模型说完成”。完成判断必须遵循：

```text
机器验证结果 > feature 状态 > evaluator 意见 > agent 自报完成
```

MVP 的核心价值：把当前需要用户或 agent 手动触发的 L2–L5 能力串成闭环，同时用轮次、时间、失败次数和人工授权等硬边界控制成本与风险。

---

## 2. 背景与问题

当前 Harness 已实现：

- `harness/features/`：feature 状态与证据；
- `harness/verification/`：受控验证命令；
- `harness/clean/`：off/warn/enforce 清洁检查；
- `harness/evaluation/`：只读 evaluator（mimo-v2.5-pro）；
- `complete_task()`：关联 feature 必须 passing 且证据未过期。

但它们仍是“可调用的能力”，没有自动编排。典型使用仍需要人工要求：

```text
创建 feature → claim → 让 agent 修改 → verify → 处理失败 → clean check → complete
```

因此存在三个问题：

1. 用户必须每次提醒 agent 使用 feature/verification 流程；
2. 没有跨会话保存“自主目标正在执行到哪”；
3. 没有统一的硬停止策略，无法安全地让 Harness 连续推进。

---

## 3. 目标与非目标

### 3.1 MVP 目标

1. `/goal --verify "<命令>" -- <目标>` 启动一个自主目标（MVP 强制显式完成判据）；
2. 自动创建一个 task 和一个 feature（MVP 固定**一目标一 feature**）；
3. WIP=1：同一时刻最多一个 active feature；
4. 自动执行 `CLAIM → ACT → VERIFY → CLEAN_CHECK`；
5. 验证失败时可有限重试；
6. 支持 `/goal status|pause|resume|cancel`；
7. goal 状态原子落盘，可跨会话恢复；
8. 有明确硬停止条件，并记录 `stop_reason`；
9. CLI 和 event-stream/TUI 语义一致；
10. 新增 G 系列零 LLM 评测，完整覆盖状态机、持久化、熔断和命令语义。

### 3.2 明确不做（MVP）

- 不从一个自然语言目标自动拆成多个 feature；
- 不做并行 feature / 并行 agent；
- 不自动创建或合并 git worktree；
- 不自动提交 git；
- 不实现 evaluator 返修循环；
- 不做 token 精确预算；
- 不做跨进程分布式锁；
- 不重写现有 `agent_loop` 的核心工具循环；MVP 必须做两个小型通用扩展：`disabled_tools`（ACT 阶段隐藏 goal/feature 编排工具）和最小 `LoopStats`（至少 `llm_rounds/stop_reason/interrupted`，用于区分正常结束与 max_rounds/cancel）；不要求 token/tool-call 精确统计；
- 不把 `/goal` 设计成执行 mode（不要加入 `config/modes.json`）；它是命令/运行器，不是 prompt mode。

> 原则：先证明“一目标一 feature 的有限自主闭环”可靠，再扩展多 feature 拆解、worktree、evaluator 返修和 token 预算。

---

## 4. 用户体验与命令语义

### 4.1 命令

```text
/goal --verify "pytest -q" -- <目标文本>    启动新 goal（`--` 后全部视为目标文本）
/goal status                              查看当前 goal 状态
/goal pause                               请求暂停（当前 agent_loop 回合协作式停止后落盘 paused）
/goal resume                              从落盘状态继续
/goal cancel                              取消 goal（终态，不删除历史）
```

可选限制参数（MVP 使用标准库 `argparse` 或等价的确定性解析；不要求 picker）：

```text
/goal --verify "pytest -q" --max-rounds 20 --timeout 1800 --max-failures 3 -- 修复分页边界
```

裸 `/goal <目标>` 在 MVP 返回用法错误，不尝试从 `HARNESS.md` 或模型推断验证命令；自动推断属于 v2。

默认值：

```text
max_rounds_per_attempt = 20
max_attempts = 3
max_consecutive_failures = 3
max_duration_seconds = 1800   # 30 分钟
verification = 必填 `--verify`（MVP 不从 HARNESS.md 或模型自动推断）
```

### 4.2 启动前置条件

`/goal --verify "<命令>" -- <目标>` 必须满足：

- 当前 workspace 可写且是有效 Git 仓库，存在可解析的 `HEAD`（MVP 的 stale/no-progress 判定依赖 git snapshot）；
- 没有 `running/pausing` 的 goal；
- 目标文本非空；
- 有明确验证命令（`--verify`，MVP 不让模型凭空决定完成判据）；
- 验证命令通过 `check_verification_command()` 的静态策略校验；
- 当前普通 agent turn 未在运行；
- 启动时记录 `workspace_generation()`，运行期间发生 `/open` 或 workspace generation 变化即停止。

不满足时拒绝启动，不创建 task/feature。

### 4.3 输出示例

启动：

```text
Goal started: goal_... [INITIALIZE]
Target: 修复分页边界问题
Verification: pytest tests/test_pagination.py -q
Limits: rounds/attempt=20, attempts=3, timeout=1800s
```

状态：

```text
Goal goal_... [VERIFY]
Feature: feat_... [active]
Attempt: 2/3
Elapsed: 96s / 1800s
Consecutive failures: 1/3
Last transition: ACT -> VERIFY
Last error: pytest failed with exit code 1
```

终止：

```text
Goal stopped [failed]
Reason: max_consecutive_failures
Feature: feat_... [failing]
Evidence: .features/feat_....json
Resume: fix the blocker, then /goal resume
```

---

## 5. 状态机

### 5.1 状态枚举

```python
class GoalPhase(str, Enum):
    INITIALIZE = "initialize"
    SELECT_FEATURE = "select_feature"
    CLAIM = "claim"
    ACT = "act"
    VERIFY = "verify"
    EVALUATE = "evaluate"       # 条件阶段；MVP 只运行一次，不返修
    CLEAN_CHECK = "clean_check"
    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
```

终态：`DONE / CANCELLED / FAILED`。  
可恢复暂停态：`PAUSED`。

### 5.2 正常路径

```text
INITIALIZE
  → SELECT_FEATURE
  → CLAIM
  → ACT
  → VERIFY
      ├─ verification passed → [EVALUATE?] → CLEAN_CHECK → DONE
      └─ verification failed → ACT（attempt+1）
```

### 5.3 状态转移表

| 当前阶段 | 条件 | 下一阶段 | 持久化动作 |
|---|---|---|---|
| INITIALIZE | task/feature 创建成功 | SELECT_FEATURE | 写 task_id、feature_id |
| SELECT_FEATURE | feature=not_started | CLAIM | 写 current_feature_id |
| SELECT_FEATURE | feature=active/failing | ACT | 恢复现有工作 |
| SELECT_FEATURE | feature=passing 且 fresh | CLEAN_CHECK | 不重复 ACT |
| SELECT_FEATURE | feature=passing 但 stale | ACT | `reopen_feature()` |
| CLAIM | claim 成功 | ACT | feature active |
| ACT | agent_loop 正常结束 | VERIFY | attempt+1，记录轮次摘要 |
| ACT | pause/cancel/硬停止 | PAUSED/CANCELLED/FAILED | 写 stop_reason |
| VERIFY | passing | EVALUATE（如 required）或 CLEAN_CHECK | feature passing+evidence |
| VERIFY | failing 且可重试 | ACT | consecutive_failures+1 |
| VERIFY | 达到失败上限 | FAILED | stop_reason=max_failures |
| EVALUATE | evaluator 完成/解析失败 | CLEAN_CHECK | evaluation 落 feature；advisory |
| CLEAN_CHECK | hard checks pass | DONE | completed_at |
| CLEAN_CHECK | hard checks fail 且可重试 | ACT | last_error=clean report |
| CLEAN_CHECK | 达到失败上限 | FAILED | stop_reason=clean_failed |
| PAUSED | `/goal resume` | SELECT_FEATURE | resumed_at |

### 5.4 完成判定

`DONE` 必须同时满足：

```text
feature.state == "passing"
AND not feature_is_stale(feature)
AND run_clean_check(workspace, mode="enforce").ok
AND (not evaluation_required OR evaluation 已运行且有结构化结果)
```

说明：

- evaluator 仍为 advisory；`evaluation.passed == false` 在 MVP 不自动改 feature 状态，但 goal 最终摘要必须高亮 findings；
- 若希望 evaluator 失败阻止 DONE，属于 v2 policy，不在 MVP 暗中加入；
- agent 的文本回复不参与完成判断。

---

## 6. 持久化数据模型

### 6.1 存储位置

```text
<workspace>/.project/goal.json
<workspace>/.project/goal-history/<goal_id>.json
```

- `goal.json`：当前 goal 单槽；
- 进入终态后把完整副本写入 history，当前槽保留最近终态用于 status；
- 所有写入使用临时文件 + `os.replace`（复用 feature 原子写模式）；
- `/open` 切换 workspace 后自然读取新 workspace 的 goal，不共享全局状态。

### 6.2 GoalState

```python
@dataclass
class GoalState:
    schema_version: int
    id: str
    target: str
    verification: str
    phase: str
    status: str                 # running / pausing / paused / done / failed / cancelled
    workspace: str
    task_id: str | None
    feature_id: str | None

    max_rounds_per_attempt: int
    max_total_rounds: int       # 默认 max_rounds_per_attempt * max_attempts
    max_attempts: int
    max_consecutive_failures: int
    max_duration_seconds: int

    attempts: int
    consecutive_failures: int
    no_progress_count: int
    total_llm_rounds: int       # MVP 可由 messages assistant 增量近似
    workspace_generation: int  # 启动时的 settings.workspace_generation()
    started_at: float
    updated_at: float
    completed_at: float | None
    paused_at: float | None

    last_phase: str | None
    last_error: str | None
    stop_reason: str | None
    evaluation_required: bool
    transition_log: list[dict]
```

`transition_log` 每项：

```json
{
  "from": "act",
  "to": "verify",
  "at": 1780000000.0,
  "reason": "agent_loop_finished",
  "attempt": 1
}
```

日志最多保留最近 100 项，防止无限增长。

### 6.3 兼容与损坏处理

- 未知 `schema_version`：拒绝 resume，status 报错；
- JSON 损坏：不覆盖原文件，返回 `goal_state_corrupt`；
- workspace 不匹配：拒绝 resume；
- task/feature 缺失：进入 FAILED，`stop_reason=missing_dependency`；
- `running/pausing` 状态在进程重启后视为 `paused`，`stop_reason=process_restarted`，必须显式 `/goal resume`。

---

## 7. 模块设计

新增：

```text
harness/goal/
├── __init__.py
├── models.py       # GoalPhase / GoalStatus / StopReason / GoalState
├── store.py        # 原子读写、history、损坏检测
├── policy.py       # limits 校验、硬停止判定
├── engine.py       # 纯状态转移 + step()
├── runner.py       # 后台线程，编排 agent_loop/L2-L5
├── commands.py     # parse + handle /goal ...
└── prompt.py       # ACT 阶段注入给 agent 的单 feature 指令
```

### 7.1 `models.py`

要求：

- Enum 使用字符串值，JSON 可直接序列化；
- `GoalState` 提供 `to_dict/from_dict`；
- `StopReason` 至少包含：

```text
max_duration
max_attempts
max_consecutive_failures
max_rounds
permission_wait
no_progress
cancelled_by_user
process_restarted
missing_dependency
verification_policy_rejected
clean_check_failed
internal_error
```

### 7.2 `store.py`

公开 API：

```python
load_goal(workspace: Path | None = None) -> GoalState | None
save_goal(state: GoalState) -> None
archive_goal(state: GoalState) -> Path
clear_goal_for_test(workspace: Path | None = None) -> None
```

路径必须从 `get_workspace_paths().project_dir` 动态计算，不得导入时冻结。

### 7.3 `policy.py`

公开 API：

```python
@dataclass(frozen=True)
class StopDecision:
    stop: bool
    terminal_status: str | None
    reason: str | None
    detail: str = ""

validate_limits(state: GoalState) -> list[str]
check_stop(state: GoalState, *, now: float, cancelled: bool, permission_pending: bool) -> StopDecision
```

停止判断顺序：

```text
user cancel
→ permission pending / human decision needed
→ max duration
→ max attempts
→ max consecutive failures
→ max rounds
→ no progress
```

### 7.4 `engine.py`

`engine.py` 必须尽量纯，不直接调 LLM/文件系统：

```python
class GoalEngine:
    def initialize(self, state) -> GoalState
    def transition(self, state, target, reason, error=None) -> GoalState
    def next_phase(self, state, feature, clean_report=None) -> GoalPhase
```

非法转移抛 `GoalTransitionError`，G 系列测试直接覆盖。

### 7.5 `runner.py`

公开 API：

```python
start_goal(request: GoalRequest, *, history: list, context: dict, binding) -> GoalState
resume_goal(*, history: list, context: dict, binding) -> GoalState
pause_goal() -> GoalState
cancel_goal() -> GoalState
get_goal_status() -> str
is_goal_running() -> bool
```

并发模型：

- 每个进程最多一个 goal runner 线程；
- `threading.RLock + Event` 管理 pause/cancel；
- 不复用 `harness.agent.cancel` 的全局 cancel 作为 goal 的持久控制状态；它只用于取消当前 `agent_loop` 回合；
- runner 进入 ACT 时必须在同一控制锁内按顺序执行：确认无 pause/cancel → `clear_cancel()` → 再次确认无 pause/cancel → 标记 ACT in-flight；pause/cancel 先持锁写 Event+持久化状态，释放锁后 `request_cancel()`。这避免 pause 刚设置就被 ACT 的 `clear_cancel()` 覆盖；
- goal runner 必须独占 `agent_lock`/普通 turn：goal 运行时普通用户消息拒绝，`/goal status|pause|cancel` 与 M008 instant 配置命令仍允许；
- ACT 必须使用受限工具池：对该次 `agent_loop` 隐藏 `create_feature/claim_feature/verify_feature/evaluate_feature/complete_task/clear_tasks`，防止模型绕过 runner 或修改编排状态；保留普通读写/测试工具；
- **权限请求（MVP 保守策略）**：goal ACT 不允许进入交互式 `ask` 等待。为 goal runner 增加**线程局部**非交互权限上下文（`contextvars.ContextVar` 或 `threading.local`，不得用进程全局 bool）：任何 `ask` 直接作为工具拒绝结果返回，并把 goal 转 PAUSED（`permission_wait`）；已有 config/saved allow 正常执行，deny 正常拒绝。这样不会改变普通前台 turn 的权限行为，CLI 后台线程也不会抢 stdin，TUI 不会无限等待。交互式批准 + 120 秒 broker 超时属于 v2；
- 启动前必须先把 `harness/tasks.py` 的 `TASKS_DIR` 活动路径改成 `get_workspace_paths().tasks_dir` 动态计算（目前仅 archive 动态化）；否则 `/open` 后 goal 可能把 task 写到启动 workspace。该修复必须配机制回归测试。

### 7.6 ACT 阶段

MVP 每个 attempt 调用一次现有 `agent_loop`。先给 `agent_loop` 增加向后兼容的可选参数与最小返回统计：

```python
@dataclass
class LoopStats:
    interrupted: bool = False
    llm_rounds: int = 0
    stop_reason: str = "completed"   # completed / max_rounds / cancelled / error

# 兼容现有调用：默认仍可返回 bool；或统一返回 LoopStats 并一次性迁移调用方。
def agent_loop(..., disabled_tools: set[str] | None = None, stats: LoopStats | None = None) -> bool:
    ...
```

ACT 调用：

```python
messages.append({
    "role": "user",
    "content": build_goal_act_prompt(state, feature),
})
stats = LoopStats()
agent_loop(
    messages,
    context,
    max_rounds=state.max_rounds_per_attempt,
    binding=binding,
    disabled_tools={
        "create_feature", "claim_feature", "verify_feature",
        "evaluate_feature", "complete_task", "clear_tasks",
    },
    stats=stats,
)
state.total_llm_rounds += stats.llm_rounds
```

`get_tool_pool()` 或 `agent_loop` 内必须按 `disabled_tools` 同时过滤 schema 与 handler；不得只隐藏 schema 而保留 handler。

`build_goal_act_prompt` 必须包含：

```text
Goal target
Current feature id/name/behavior
Declared verification command
Current feature state + last_error
Attempt N/max
要求：只处理该 feature（WIP=1）；不要声称完成；结束前不要手工篡改 .features；机器验证由 runner 在 ACT 后执行
```

注意：

- ACT 阶段不要要求模型调用 `verify_feature`，避免双重编排；runner 在 ACT 返回后统一 VERIFY；
- agent 仍可跑普通 focused tests，但最终完成判据只信 runner 的 `verify_feature_command`；
- 每次 ACT 前后记录 feature JSON mtime/code snapshot；没有代码变化且 feature 错误未变化，计 `no_progress_count += 1`；连续 2 次无进展 → FAILED/PAUSED（MVP 采用 FAILED，reason=no_progress）。

### 7.7 VERIFY 阶段

```python
feature = verify_feature_command(
    state.feature_id,
    workspace=Path(state.workspace),
)
```

分支：

- passing：`consecutive_failures = 0`，进入 EVALUATE/CLEAN_CHECK；
- failing：`consecutive_failures += 1`；若未触发停止条件，进入 ACT；
- policy/permission rejection：无法靠重复 coding 修复，直接 PAUSED 或 FAILED。MVP 规定：
  - `verification_policy_rejected` → FAILED；
  - `permission_wait` → PAUSED。

### 7.8 EVALUATE 阶段

只有 `feature.evaluation_required == True` 才运行：

```python
run_evaluation(feature.id, workspace=state.workspace)
```

- 只运行一次；
- 解析失败记在 feature.evaluation.error，继续 CLEAN_CHECK；
- evaluator `passed=false` 仍可 DONE，但最终摘要必须展示 findings；
- evaluator 不得参与自动返修循环（v2）。

### 7.9 CLEAN_CHECK 阶段

强制使用 enforce，不读取环境变量：

```python
report = run_clean_check(Path(state.workspace), mode="enforce")
```

- `report.ok` → DONE；
- 不通过 → `last_error = report.summary()`，consecutive_failures+1，未达上限则 ACT；
- 损坏 feature JSON / stale passing 必须阻止 DONE（已有 L4 机制）。

---

## 8. CLI / TUI 接入

### 8.1 Classic CLI

在 `harness/cli.py` 普通消息进入 agent 前加入：

```python
if _match_cli_command(query, "/goal"):
    renderer.plain(handle_goal_command(query, history, context, binding))
    continue
```

`/goal --verify "<command>" -- <target>` 启动后台 runner，CLI 输入循环立即返回；status/pause/cancel 可继续输入。

### 8.2 Event-stream/TUI

在 `harness/event_stream.py::_handle_slash_command` 增加 `/goal` 分支。

命令分类：

```text
instant while busy:
  /goal status
  /goal pause
  /goal cancel
  /model /effort /mode ...（已有 M008）

start/resume（要求没有普通 turn running）：
  /goal --verify "<command>" -- <target>
  /goal resume
```

不要把所有 `/goal` 无条件加入 `_INSTANT_SLASH_PREFIXES`。新增精确分类函数，例如：

```python
def _is_goal_control_command(query: str) -> bool:
    sub = parse_goal_subcommand(query)
    return sub in {"status", "pause", "cancel"}
```

TUI 事件：

```text
goal_started
goal_status
goal_phase
goal_stopped
```

最小 payload：`id/status/phase/feature_id/attempt/limits/last_error/stop_reason`。

前端 MVP 不需要新页面；先把事件渲染为 log/status 行。可在后续版本增加 goal panel。

### 8.3 普通消息与配置切换

- goal running 时：普通 `user_message` 返回 `Goal is running. Use /goal status|pause|cancel.`；
- `/model /mode /effort` 仍允许即时切换；新模型只影响下一次 ACT 的 LLM 调用，不中断当前 API 请求；
- `/open /resume /clear` 在 goal running/pausing 时拒绝，防止 workspace/session 漂移；
- `/goal pause` 完成后才允许 `/open`。

---

## 9. Feature / Task 创建策略（MVP）

启动成功后，INITIALIZE 创建：

```python
task = create_task(
    subject=f"Goal: {short_target}",
    description=target,
)
feature = create_feature(
    name=short_target,
    behavior=target,
    verification=verification,
    workspace=workspace,
    task_id=task.id,
    evaluation_required=request.evaluation_required,
)
attach_feature(task.id, feature.id)
claim_task(task.id, owner="goal:<goal_id>")
```

随后状态机进入 SELECT_FEATURE/CLAIM。

约束：

- 一个 goal 只创建一个 task、一个 feature；
- 初始化失败必须回滚本次刚创建的 task/feature，或将 task 标记 cancelled 并记录 `internal_error`；
- 不复用任意旧 active task，避免目标粘连；
- goal DONE 后调用 `complete_task(task.id)`；如果 complete_task 因门控失败，goal 不能标 DONE，应进入 FAILED（`clean_check_failed`）。

---

## 10. 硬停止策略

### 10.1 必做条件

| 条件 | 默认 | 结果 |
|---|---:|---|
| 总时长达到 max_duration_seconds | 1800s | FAILED / max_duration |
| ACT attempts 达 max_attempts | 3 | FAILED / max_attempts |
| 总 LLM 轮次达到 max_total_rounds | 60 | FAILED / max_rounds |
| 连续 VERIFY/CLEAN 失败 | 3 | FAILED / max_consecutive_failures |
| 单次 ACT 达 max_rounds_per_attempt | 20 | 结束该 ACT 并进入 VERIFY；LoopStats.stop_reason=max_rounds |
| 连续无代码/错误进展 | 2 次 | FAILED / no_progress |
| 用户 pause | — | PAUSED |
| 用户 cancel | — | CANCELLED |
| ACT 工具权限决策为 ask（非交互 goal） | 首次出现 | PAUSED / permission_wait |
| workspace 变化 | — | FAILED / workspace_changed |
| task/feature 缺失或损坏 | — | FAILED / missing_dependency |

### 10.2 为什么不以 agent 自报完成停止

模型回复“已完成”只表示 ACT 回合结束。runner 仍必须执行 VERIFY 和 CLEAN_CHECK。即使模型明确说 DONE，验证失败仍返回 ACT 或 FAILED。

### 10.3 成本边界

MVP 不做 token 精确预算；通过以下硬边界控制成本：

```text
max_rounds_per_attempt × max_attempts
+ max_duration_seconds
+ max_consecutive_failures
+ no_progress_count
```

v2 再给 `agent_loop` 增加 `LoopStats/observer`，记录精确 rounds/tool_calls/tokens。

---

## 11. 安全与权限

1. `/goal` 不新增绕过权限的执行路径；ACT 完全复用普通工具池和 `PreToolUse` 权限 hook；
2. VERIFY 复用 `verify_feature_command`（shell=False、verify_command allow、工作区变更检测）；
3. `.features` 继续受 deny 保护；
4. goal state 位于 `.project/`，普通 agent 不应直接修改；建议补 permissions deny：

```json
"write_file": {
  ".project/goal.json": "deny",
  ".project/goal-history/*": "deny"
}
```

并在 `permission_hook` 对 `write_file/edit_file/bash` 访问 goal 状态文件做 config deny 红线；只有 `harness.goal.store` 内部可写。

5. runner 不得自动选择 destructive 权限；ask 需要用户确认，超时暂停；
6. cancel 是协作式：当前 provider 调用无法立即强杀时，等调用返回后检查 cancel，不再进入下一阶段。

---

## 12. 测试规格（G 系列）

新增：`evals/cases/goal.py`，注册到 `evals/runner.py`。默认零 LLM，所有 agent/verification/evaluator 调用 mock；临时 workspace，不能污染真实 `.project/.tasks/.features`。

### 12.1 必测用例

| ID | 测试 | 核心断言 |
|---|---|---|
| G001 | legal transitions | 正常 phase 转移通过，非法转移抛 GoalTransitionError |
| G002 | initialize creates graph | 创建 1 task + 1 feature，双向 id 绑定，WIP=1 |
| G003 | verification pass → done | VERIFY passing + clean ok → DONE，task completed |
| G004 | verification fail retries | failing → ACT，attempt/failure 累加，不超过上限 |
| G005 | max failures fuse | 达上限 → FAILED，stop_reason 正确，不再调用 agent |
| G006 | max duration fuse | fake clock 超时 → FAILED |
| G007 | no progress fuse | 连续两次 snapshot/error 不变 → FAILED/no_progress |
| G008 | pause/resume | ACT 中 pause → PAUSED；resume → SELECT_FEATURE；状态落盘 |
| G009 | cancel | cancel → CANCELLED，后续 step 不执行 |
| G010 | process restart recovery | 落盘 running 状态加载后转 PAUSED/process_restarted |
| G011 | WIP=1 | 已有一个 active feature 时不得 claim 第二个（MVP 只有一个） |
| G012 | stale passing | passing 后代码变更 → ACT/reopen，不 DONE |
| G013 | clean failure | clean enforce fail → ACT 或达到上限 FAILED |
| G014 | evaluator advisory | evaluation_required 运行一次；false findings 记录但不改 feature state |
| G015 | command parsing | start/status/pause/resume/cancel 参数校验正确 |
| G016 | TUI busy controls | goal running 时 status/pause/cancel 可用，普通消息/open 被拒 |
| G017 | atomic store | 模拟 os.replace 失败，旧 goal.json 完整、tmp 清理 |
| G018 | corrupt state | 损坏 JSON 不覆盖，status 报 corrupt，resume 拒绝 |
| G019 | workspace isolation | A/B goal 状态互不泄漏，/open 后读取 B |
| G020 | non-interactive permission ask | ask 不阻塞/不读 stdin，立即 PAUSED/permission_wait |
| G021 | model switch during goal | /model 即时切换不取消 goal；下一 ACT 使用新模型 |
| G022 | no agent self-completion | mock agent 回复 DONE，但验证失败 → 不 DONE |

### 12.2 测试污染硬判据

测试前后必须断言：

```text
真实 .project/goal.json 不变
真实 .tasks/archive 文件数不变
真实 .features 文件数不变
```

### 12.3 回归标准

```text
python -m evals
→ 原 71 项全部保持 pass
→ G001–G022 全 pass
→ fail=0
```

可选 live 冒烟（不进默认 CI）：

```text
python -m evals --live --category goal
```

真实 LLM 冒烟只用受控 fixture，`max_attempts=1`、`max_rounds=5`、timeout=120s。

---

## 13. 实施顺序（下一会话照此执行）

### Step 1：先写状态与 store（红→绿）

1. 新建 `harness/goal/models.py`、`store.py`；
2. 写 G001/G010/G017/G018/G019；
3. 做原子写、schema version、restart recovery；
4. 先跑 goal 类测试。

### Step 2：纯 engine + policy

1. 新建 `engine.py`、`policy.py`；
2. 写 G004–G007/G011/G012/G013/G022；
3. 状态转移与停止判断保持纯函数。

### Step 3：runner 编排 L2–L5

1. 新建 `prompt.py`、`runner.py`；
2. 接 `create_task/create_feature/attach_feature/claim_task`；
3. ACT 接 `agent_loop(max_rounds=...)`；
4. VERIFY 接 `verify_feature_command`；
5. EVALUATE 接 `run_evaluation`；
6. CLEAN 接 `run_clean_check(..., mode="enforce")`；
7. 写 G002/G003/G008/G009/G014/G020。

### Step 4：命令与 CLI/TUI

1. 新建 `commands.py`；
2. 接 `harness/cli.py`；
3. 接 `harness/event_stream.py` 和最小事件；
4. 更新 `_is_instant_slash_command` 的精确分类；
5. 写 G015/G016/G021。

### Step 5：安全、回归、文档

1. deny `.project/goal*.json` 普通写入；
2. 运行 `python -m evals`；
3. 做一次受控 live 冒烟；
4. 更新 `harness-reliability-plan.md` L6 状态、changelog、失败案例；
5. 单独 commit（不要混入无关文件）。

---

## 14. Definition of Done

只有全部满足才算 L6 MVP 完成：

```text
[ ] /goal <target> --verify <command> 能启动并后台推进
[ ] /goal status/pause/resume/cancel 在 CLI + TUI 均可用
[ ] 一个 goal 只创建一个 task + 一个 feature（MVP）
[ ] WIP=1 有机器约束
[ ] ACT 后一定由 runner 执行 VERIFY，不采信 agent 自报完成
[ ] feature passing + fresh + clean enforce pass 才能 DONE
[ ] 所有硬停止条件可触发且 stop_reason 可追溯
[ ] 进程重启后 running goal 转 paused，可显式 resume
[ ] goal state 原子写，损坏文件不被覆盖
[ ] 权限等待不永久挂死；cancel 不再进入下一阶段
[ ] goal 运行时普通消息/open/resume/clear 被正确限制
[ ] /model /mode /effort 仍可即时切换，不触发 running UI（M008 不回归）
[ ] G001–G022 全通过
[ ] python -m evals 100%，fail=0
[ ] 默认评测不调用 LLM、不污染真实 workspace
[ ] 文档和帮助文本已更新
```

---

## 15. 已知限制与 v2 路线

**v2 第 1 项已实现（2026-08-06）：目标自动拆解成多个 feature + 依赖图**
（`harness/goal/planner.py` + runner 依赖序编排 + FULL_VERIFY 全量兜底 + per-feature 预算 +
**拆解确认闸**（多 feature 拆解后 PAUSED 等 `/goal resume` 批准）；G024–G029；
详见 reliability plan L6 v2 实施记录）。

MVP 验证后再考虑（其余项）：

1. 目标自动拆解成多个 feature + 依赖图；
2. worktree 自动创建/恢复/合并；
3. `LoopStats/observer` 精确统计 token/tool calls/rounds；
4. evaluator findings 驱动有限返修（最多 1–2 轮）；
5. token budget 和成本预测；
6. goal 事件的专用 TUI 面板；
7. 多进程锁与远程恢复；
8. 用户批准的 checkpoint/commit 策略。

扩展原则：只有真实 MVP 运行数据证明需要，才增加复杂度。

---

## 16. 下一会话启动提示

新会话直接发送：

```text
请阅读 docs/goal-mode-mvp-spec.md，并严格按第 13 节实施顺序实现 /goal MVP。
先检查当前 git status 和现有接口；一次只做一个 Step；每个 Step 先写对应 G 系列红测试再实现；不要实现第 3.2 节明确排除的 v2 功能。完成后运行 python -m evals，并按 Definition of Done 逐项验收。
```

实现前还应先读：

```text
HARNESS.md
ARCHITECTURE.md
harness/loop.py
harness/features/
harness/verification/
harness/clean/
harness/evaluation/
harness/tasks.py
harness/cli.py
harness/event_stream.py
```
