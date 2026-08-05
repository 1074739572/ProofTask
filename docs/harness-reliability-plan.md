# Harness 可靠性改进 · 总计划（2026-08 起）

> 本文是「借鉴 Learn Harness Engineering 课程 → 审查现有 harness → 逐层实施」的总记录。
> 状态跟踪见文末「实施状态」；设计细节按层放在各章节。
> 相关文档：[README](../README.md) · [ARCHITECTURE](../ARCHITECTURE.md) · [evals.md](./evals.md) · [project-instructions.md](./project-instructions.md)

---

## 0. 来龙去脉

### 0.1 起点

用户在网上找到课程站 **Learn Harness Engineering**（中文版）：

- https://walkinglabs.github.io/learn-harness-engineering/zh/

课程参考了 OpenAI《Harness engineering: leveraging Codex in an agent-first world》、
Anthropic《Effective harnesses for long-running agents》《Harness design for long-running application development》等文章。

### 0.2 我们做了什么

1. 抓取并通读全部 **13 讲讲义**（L01 模型强≠执行可靠 → L13 Loop Engineering）。
2. 对照本仓库 `improved_harness`（基于 shareAI-lab/learn-claude-code 的 s20 单文件版拆分改进而来）
   审查了 `harness/loop.py`、`harness/todos/`、`harness/tasks.py`、`harness/hooks.py`、
   `harness/prompts/project_md.py`、`config/agents.json`、`evals/` 等关键模块。
3. 核验了官方依据：Anthropic《Effective harnesses for long-running agents》(2025-11-26) 原文已读；
   **未找到** Claude Code / Codex 官方 `/goal` 文档，也未找到课程所述三 Agent 实验的官方原文 →
   这两者视为「待验证设计」，不作为直接依据。
4. 得出总体判断：现有 harness 在「五子系统」的量上已较完整，缺的是**关键接缝上的闭环**——
   完成门控、状态分层、退出清洁、独立评估、自主循环。
5. 产出七层改进计划（下节），并决定**先做评测层（L0）**，用数据驱动后续每一层。

### 0.3 关键结论（审查时修正过的点）

- 不要把会话 todo 直接改造成验证器；todo / task / feature 是**三层不同状态**（详见 L2）。
- 完成判定必须外部化：agent 自报完成 ≠ 实际完成，评测必须分开记录两个字段。
- `/goal` 不是「重复调 agent_loop 直到模型说完成」，必须有显式状态机 + 硬停止条件。
- 验证命令不能自动执行任意 shell 字符串，必须走现有权限系统并受控（超时/目录/输出）。
- 清洁检查不要挂普通 `Stop` hook（每轮触发），应放在任务完成、`/goal` 结束或显式 `/check`。

---

## 1. 总体计划：七层体系

依赖方向（下层是上层的地基，不能跳层建设）：

```text
L0 评测与可观测性
        ↓
L1 项目指令与上下文（HARNESS.md 路由页）
        ↓
L2 三层状态模型（todo / task / feature）
        ↓
L3 确定性验证门控
        ↓
L4 清洁状态与交接
        ↓
L5 独立评估器
        ↓
L6 /goal 自主循环
```

### 优先级与顺序（一次只做一层，每层做完跑 L0 对照）

| 层 | 内容 | 优先级 | 依赖 |
|----|------|--------|------|
| L0 | Harness 可靠性评测框架 | P0 | 无 |
| L1 | HARNESS.md 路由页 + 冷启动检查 | P0 | L0（评测可禁用它做 baseline） |
| L2 | todo / task / feature 三层状态分离 | P0 | L0 |
| L3 | Feature 验证门控（oracle 化） | P0/P1 | L2 |
| L4 | 清洁状态与交接（off/warn/enforce） | P1 | L3 |
| L5 | 独立 evaluator（小范围试点） | P2 | L3、L4 |
| L6 | /goal 自主循环（状态机 + 熔断） | P3 | L0–L5 |

---

## 2. L0 评测层设计（第一个实施）

### 2.1 目的与定位

现有 `evals/cases/` 回答「harness 的代码有没有坏」（回归体检）；
新评测回答「**同一个模型、同一个任务，不同 harness 配置下，执行可靠性差多少**」。
两者不混在一起。

新增独立评测域（与 `evals/swebench/`、`evals/gaia/` 同级）：

```text
evals/
├── cases/                   # 现有基础回归，不动
├── swebench/                # 现有真实代码任务，不动
├── gaia/                    # 现有通用工具任务，不动
└── harness_reliability/     # 新增：Harness 可靠性评测
    ├── __init__.py
    ├── types.py             # ReliabilityTask / HarnessVariant / ReliabilityRun
    ├── runner.py            # 单任务运行器（复制 fixture → 跑 agent → 调 oracle）
    ├── workspace.py         # fixture 复制与隔离
    ├── oracle.py            # 独立验收（隐藏检查）
    ├── metrics.py           # 汇总指标
    ├── report.py            # 终端报告 + JSON
    ├── fixtures/            # 受控测试项目
    └── __main__.py          # python -m evals.harness_reliability
```

### 2.2 与课程文章的对应

| 文章部分 | 评测怎么测 |
|---|---|
| L01 模型强≠执行可靠 | 同模型同任务，对比不同 harness 配置 |
| L02 五子系统 | 分别关闭指令/状态/反馈，测性能变化（消融） |
| L05 跨会话连续性 | 有无进度文件时的新会话恢复成本 |
| L07/L08 WIP=1 与功能清单 | 测是否过度扩展、只完成半个功能 |
| L09/L10 防止提前完成、端到端验证 | 测「声称完成」与「实际通过」偏差 |
| L11 可观测性 | 保存轨迹、工具调用、验证结果、失败原因 |

核心原则：**不能只让 agent 自己说「我完成了」，必须由评测器（oracle）单独判断。**

### 2.3 第一阶段：5 类固定任务（fixtures）

用**很小、完全受控**的测试项目（几十到几百行），不引入复杂项目：
`evals/harness_reliability/fixtures/user_service/`（app.py + tests + requirements + README）。

| ID | 任务 | 测什么 | 对应文章 |
|----|------|--------|----------|
| H001 | 单文件修复（分页边界 bug） | 定位问题、是否改无关文件、是否跑测试、是否准确报告完成 | L01/L09 |
| H002 | 跨模块功能（用户偏好设置） | 理解模块关系、遵守架构、集成测试、接口边界 | L10/L02 |
| H003 | 容易过度扩展（加邮箱格式校验） | 是否顺便重构/改库/加服务/改中间件 | L07/L08 |
| H004 | 跨会话任务（模型+接口分两段） | 恢复成本、重复实现、推翻决策、功能完成数 | L05/L06/L12 |
| H005 | 会误导提前宣布完成（密码重置） | 单元测试不足时，隐藏验收是否通过 | L09/L10/L11 |

### 2.4 数据模型

```python
# types.py
@dataclass(frozen=True)
class ReliabilityTask:
    id: str
    name: str
    category: str
    prompt: str
    fixture: str
    max_rounds: int = 25
    requires_multi_session: bool = False

@dataclass(frozen=True)
class HarnessVariant:
    id: str
    project_instructions: bool
    progress_state: bool
    verification_prompt: bool
    wip_constraint: bool
    clean_state: bool = False

@dataclass
class ReliabilityRun:
    task_id: str
    variant_id: str
    run_id: str
    status: str                 # ok / error / timeout / interrupted
    claimed_complete: bool      # agent 自报完成（必须与 oracle 分开）
    oracle_passed: bool         # 独立验收通过
    tool_calls: int
    llm_rounds: int
    input_tokens: int
    output_tokens: int
    duration_ms: float
    files_changed: list[str]
    retries: int
    human_interventions: int
    recovery_minutes: float | None   # 跨会话任务专用
    failure_reasons: list[str]
    transcript_path: str
```

### 2.5 核心指标

1. **实际完成率** `oracle_passed / total`（主指标，不能用自报）
2. **虚假完成率** `(claimed && !oracle_passed) / claimed`（L09 验证缺口）
3. **验证执行率** 实际执行规定验证命令的次数 / 总数（注意 ≠ 通过率）
4. **过度扩展率** 修改无关文件/启动额外功能的运行占比（oracle 比对 allowed_paths）
5. **WIP 指标** 一次运行同时启动的方向数（第一版用规则估计；有 feature 状态机后算 max_concurrent_features，目标 ≤1）
6. **跨会话恢复成本** 第二会话开始 → 首次有效修改/验证的时间；重复探索调用数
7. **工具效率** tool_calls / failed / repeated / permission_denials（关联现有 RepeatGuard 等）

### 2.6 Oracle（评测的核心）

- 独立于 agent 运行：pytest / 静态检查 / 集成测试 / 行为脚本 / git diff 范围检查。
- agent 不能修改 oracle 与隐藏验收。
- 输出 `OracleResult(passed, checks[], evidence_paths[], failure_reasons[])`。

### 2.7 三种 Harness 配置（第一版）

| Variant | HARNESS.md | progress/feature | 验证要求 | 用途 |
|---------|-----------|------------------|----------|------|
| baseline | 无 | 无 | 无 | 裸跑基线 |
| instructions | 有 | 无 | 有 | 测「仓库即规范」 |
| structured | 有 | 有 | 有 | 测状态+验证组合 |

第二版再做消融：`no-instructions / no-state / no-feedback / no-scope`（一次只移除一个，才能归因）。

### 2.8 复用现有代码

- `agent_loop`（`harness/loop.py`）：已支持 max_rounds / 权限 / compact / recovery。
- `switch_workspace()`（`harness/workspace.py`）：已有现成的进程内工作区切换，
  **不要**复制 SWE-bench 那种 patch 多个 module-level WORKDIR 的做法（脆弱）。
- `TurnMutationTracker`（`harness/ui/turn_summary.py`）：已有文件变更跟踪，
  但只在一次 agent_loop 内部；给 `agent_loop` 增加**可选 observer/LoopStats** 参数，不破坏现有调用。
- token：`harness/usage/store.py` 按日全局记录；第一版用**前后快照差值**，暂不改全局 ledger。
- transcript：复用 `.project/sessions/<id>/session.jsonl`，结果里只存 `transcript_path`。

### 2.9 结果目录结构

```text
evals/results/harness_reliability/
└── run_20260801T120000Z/
    ├── summary.json
    ├── results.jsonl
    ├── H001-baseline/
    │   ├── workspace/          # 运行后的 fixture 副本（供复现）
    │   ├── oracle.json
    │   ├── diff.patch
    │   └── transcript.json
    └── H001-structured/...
```

### 2.10 第一版先不做

- 不做 evaluator agent（先确定性 oracle；它本身也需要被评测）。
- 不做 `/goal`。
- 不把所有历史任务拉进来（先 5–10 个固定任务）。

### 2.11 第一版成功标准

```text
[ ] 能在干净 fixture 中运行 agent
[ ] 能保存 transcript / diff / oracle 结果
[ ] 能区分 agent 自报完成与实际完成
[ ] 能统计 token / 回合数 / 工具调用 / 文件修改
[ ] 能比较 baseline / instructions / structured
[ ] 能重复运行并生成汇总报告
[ ] 至少有一个任务稳定暴露 baseline 的失败
[ ] 不修改真实项目代码；评测结果不污染当前工作区
```

### 2.12 环境坑：Windows 下 python 挂起（stdin 未关闭）

排查记录：本机通过 shell 工具运行 `python -c` / 脚本时 CPU 0、无输出、疑似卡死；
`python --version` 却秒回。定位结论（用户提示换行方向后验证）：

- **根因不是换行，是 stdin**：命令的 stdin 是一个保持打开的管道，python（及子进程）
  会进入「等待输入」状态；`python -c "print(1)" < NUL` 立即正常输出，`echo print(1) | python`
  也能正常执行，证明 stdin 关闭后一切正常。
- **修复**：所有手工命令加 `< NUL`；评测框架内部 subprocess 一律
  `stdin=subprocess.DEVNULL`（见 `oracle.py` / `workspace.py` / `runner.py`）。
- 附加：Windows 上 git 对象文件只读，重跑前删除工作区要用
  `_force_rmtree`（先 chmod 再删），否则 `shutil.rmtree` 报 PermissionError。

期望输出形态：

```json
{
  "task_id": "H003",
  "variant": "baseline",
  "runs": 3,
  "completion_rate": 33.33,
  "false_completion_rate": 66.67,
  "avg_tool_calls": 21.3,
  "avg_input_tokens": 18200,
  "avg_duration_s": 310.5
}
```

---

## 3. L1 路由页设计（第二个实施）

### 3.1 定位

**路由页 = `HARNESS.md`，是 Agent 进入项目后的地图和入口，不是完整规则库。**

加载机制已存在（`harness/prompts/project_md.py`，无需重写）：

- 从 WORKDIR 向上查找，`HARNESS.md` 优先于 `AGENTS.md`，最近目录优先；
- 默认最多 12,000 字符，超出截断并警告；`HARNESS_PROJECT_MD=0` 可禁用；
- 注入为 ephemeral session context（每轮模型调用可见）。

第一版只在**仓库根目录**放一份；子目录放同名文件会遮蔽根文件，先不用。

### 3.2 解决的问题：全新会话的六个问题

```text
1. 这是什么项目？
2. 从哪里开始读？
3. 怎么安装和运行？
4. 怎么验证修改？
5. 哪些规则绝对不能违反？
6. 不同类型的任务去读哪些文档？
```

对应文章：L02 指令子系统 / L03 仓库即规范 / L04 入口文件是路由器 / L06 初始化 /
L09-L10 完成定义与验证命令 / L12 会话退出规则。

### 3.3 推荐结构（模板）

```markdown
# Project
一句话项目说明 + 主入口。

## Start Here
- 先读 `README.md`；架构读 `ARCHITECTURE.md`；工具/权限读 `docs/tools.md`；评测读 `docs/evals.md`

## Commands
- Install / Unit tests / Full check / Start（必须是真实存在且跑过的命令）

## Hard Constraints
- 保持单一 agent_loop 主循环；修改工具同步 schema+handler+权限；不绕过权限系统；
  不修改已有测试掩盖失败；跨模块修改必须跑对应测试；未验证不得声称完成
（只放红线，≤10–15 条）

## Task Routing
- 修改主循环 → ARCHITECTURE.md + harness/loop.py（必读）
- 修改工具/权限 → docs/tools.md（必读）
- 修改评测 → docs/evals.md + evals/（必读）
- …（按仓库实际补充）

## Definition Of Done
- 行为修改有对应测试；相关测试通过；无新增未说明失败；修改范围符合任务；
  最终回复含修改文件、验证命令、结果
（代码修改完成 ≠ 任务完成）
```

### 3.4 各章节要点

| 章节 | 要点 |
|------|------|
| Project | 2–4 行；不重复 README |
| Commands | 最核心；没有验证命令 agent 会用「代码看起来没问题」代替验证 |
| Hard Constraints | 措辞明确（必须/禁止/只能）；能被测试或 lint 检查；不放个人偏好与历史闲聊 |
| Task Routing | 最有价值；`什么任务 → 读哪个文件 → 是否必读`；agent 需自行 read_file，系统不会自动全加载 |
| Definition of Done | 把「代码写完」和「任务完成」分开；写可执行条件，不写「看起来正确」 |

### 3.5 与评测的关系

在 L0 评测中，HARNESS.md 是一个**独立变量**：

```text
baseline       → HARNESS_PROJECT_MD=0（或 fixture 不含该文件）
instructions   → 只启用 HARNESS.md
structured     → HARNESS.md + progress.md + feature_list.json
```

**注意：收集 baseline 之前，不要把 HARNESS.md 加进所有评测工作区，否则基线被污染。**

### 3.6 实现优先级

| 优先级 | 事项 |
|--------|------|
| P0 | 根目录新增 `HARNESS.md`（概览/真实命令/硬约束/文档路由/完成定义） |
| P0 | `evals/cases/` 增加路由页静态检查（存在、行数、章节、链接指向真实文件、命令非空） |
| P1 | 冷启动评测：新会话只看仓库，回答项目是什么/怎么跑/怎么测/架构入口/从哪开始（L03 全新会话测试） |
| P1 | 文档衰减检查：命令仍可执行、链接仍存在、描述不过期 |

### 3.7 避免的设计

- 600 行历史经验 / 每个模块详细实现 / bug 完整复盘 / 个人编码偏好 / 过期的决策 / 所有工具 schema / 假验证命令。
- 目标：**少量内容让 agent 找对方向**，不是把整个仓库知识塞进上下文。

### 3.8 完成标准

```text
[ ] 根目录存在 HARNESS.md
[ ] 全新会话能找到项目入口
[ ] 全新会话能找到真实测试命令
[ ] 关键规则都能在仓库内继续追踪
[ ] 文件 ≤ 约 100–150 行
[ ] 所有链接指向真实文件
[ ] 命令至少被人工执行过一次
[ ] baseline 评测可以禁用它；instructions 评测可以只启用它
```

核心原则：**HARNESS.md 负责导航，docs/ 负责知识，测试和 lint 负责强制执行。**
高风险规则最终应落到可执行检查，而不是只写在文档里。

---

## 4. 后续层（先记录，逐层展开）

- **L2 三层状态模型**（✅ 已实施，2026-08-05，详见 §5 L2 实施记录）：todo（会话内步骤）/ task（跨会话调度，`harness/tasks.py` 扩展 feature_ids、attempts、last_error）/ feature（新原语：behavior + verification + state + evidence，JSON 文件原子写，状态机 not_started→active→passing/blocked/failing）。feature 状态只能由验证器更新为 passing；已通过可因重验失败退回 failing（不要设计成绝对不可逆）。
- **L3 确定性验证门控**（✅ 已实施，2026-08-05，详见 §5 L3 实施记录）：`harness/verification/`（policy/runner/evidence/__init__）；agent 请求验证 → 权限与命令策略检查 → 受控执行 → 记录 exit code/stdout/证据 → 更新 feature 状态。优先确定性信号：静态检查→单测→集成→启动健康→端到端。
- **L4 清洁状态与交接**（✅ 已实施，2026-08-05，详见 §5 L4 实施记录）：`harness/clean/`（checker + 三模式 off/warn/enforce，`HARNESS_CLEAN_MODE` 环境变量）；触发点 = complete_task（未挂 Stop hook）；检查临时工件 / feature 状态一致性（passing 无证据）/ git 未提交变更（软信息）；enforce 模式硬检查失败阻止任务完成；只报告不自动删除，不动用户文件。
- **L5 独立 evaluator**（✅ 已实施，2026-08-05，详见 §5 L5 实施记录）：`config/agents.json` 增加只读 `evaluator` 角色（mimo-v2.5-pro，无写工具）；`harness/evaluation/`（inputs/parser/runner）；输入含原始需求/feature 验收/diff/确定性验证结果/评分标准；输出结构化 findings（含证据）；简单任务不启用（requires_evaluation 启发式）；有限返修轮次留待 L6（已暂缓）。
- **L6 /goal 自主循环**：显式状态机 INITIALIZE→SELECT_FEATURE→CLAIM→ACT→VERIFY→CLEAN_CHECK→…→DONE；goal 状态落盘；硬停止（max rounds/时间/token/连续失败/无进展/重复调用/权限等待/需人工决策/环境不可恢复）；停止判断顺序：机器验证 > feature 状态 > evaluator > agent 自报；WIP=1；优先 worktree；支持 /goal status|pause|resume|cancel。

---

## 5. 实施状态

> 每完成一层，在此打勾并补 changelog / bug 记录。

| 层 | 状态 | 完成日期 | 备注 |
|----|------|----------|------|
| L0 评测框架 | ✅ 已完成（7 任务 + 30 次对照） | 2026-08-04 | `evals/harness_reliability/`；H006/H007 已加 |
| L1 HARNESS.md 路由页 | ✅ 已完成 | 2026-08-04 | 根 `HARNESS.md` + `evals/cases/project_md.py` 静态检查 |
| L2 三层状态模型 | ✅ 已完成 | 2026-08-05 | `harness/features/` + F001–F007 + `tasks.py` 扩展 |
| L3 验证门控 | ✅ 已完成 | 2026-08-05 | `harness/verification/` + V001–V008 |
| L4 清洁状态 | ✅ 已完成 | 2026-08-05 | `harness/clean/` + W001–W007 |
| L5 独立 evaluator | ✅ 已完成 | 2026-08-05 | `harness/evaluation/` + E001–E007 |
| L6 /goal | 未开始 | — | |

### 执行约定

- 一次只做一层，每层完成后跑 `python -m evals`（回归）确认没弄坏现有功能。
- 每层改动落 `docs/` 对应记录 + `docs/CHANGELOG-*`。
- L0 收集的 baseline 数据，作为 L1–L4 每层的对照基准。

### L2 实施记录（2026-08-05）

**产出**：

1. **`harness/features/`（新模块）**：
   - `schema.py`：`Feature`（id/name/behavior/verification/state/workspace/task_id/evidence/attempts/last_error/timestamps）、
     `VerificationEvidence`（command/exit_code/stdout_tail/duration_ms/verified_by）、`TRANSITIONS` 状态机白名单。
   - `state.py`：持久化于 `<workspace>/.features/feat_*.json`（跟随 `switch_workspace`，路径挂入
     `settings.WorkspacePaths.features_dir`）；**原子写**（同目录临时文件 + `os.replace`）；API：
     `create_feature / get_feature / list_features / claim_feature / block_feature / reopen_feature / verify_feature / clear_features`。
   - 门控规则：`verify_feature(passed=True)` **必须**携带 `exit_code==0` 的证据，否则 ValueError——
     agent 自报完成对 feature 状态无任何程序效力（对应 0.3 关键结论 2）。
   - 可逆性：`passing --重验失败--> failing`（`completed_at` 清空、`attempts+1`、记 `last_error`），
     `failing/blocked/passing --reopen--> active`。
2. **`harness/tasks.py` 扩展**：`Task` 增加 `feature_ids/attempts/last_error`（默认值向后兼容，旧 JSON 直接加载）；
   `claim_task` 递增 `attempts`；新增 `attach_feature(task_id, feature_id)`。
3. **F 系列评测（`evals/cases/features.py`，零 LLM，已注册进 `python -m evals`）**：
   F001 全生命周期 / F002 passing 必须有证据 / F003 非法转移被拒 / F004 可逆性 /
   F005 原子写不损坏 / F006 task 旧 JSON 兼容 / F007 工作区隔离。

**回归结果**：`python -m evals` = **100%（40 pass / 0 fail / 1 warn / 2 skip）**，M 系列等既有检查全部保持 PASS。

**手动演示**：`python demo_l2_features.py`（根目录）——创建 → claim → 假自报完成被拒 → 带证据置 passing → 重验失败退回 failing，5 步输出。

**实现坑**：feature id 前缀为 `feat_` 而 `list_features` 的 glob 曾写成 `feature_*.json`（拼写不一致导致列表为空），已统一为 `feat_*.json`。

**L2 完成标准自查**：
- [x] feature 状态只能由验证器写入 passing（F002）
- [x] passing 可因重验失败退回 failing（F004）
- [x] 原子写，崩溃不损坏文件（F005）
- [x] task 扩展不破坏旧数据（F006）
- [x] 按工作区隔离（F007）
- [x] todo 保持会话内职责，未被改造成验证器

### L3 实施记录（2026-08-05）

**产出**：

1. **`harness/verification/`（新模块）**：
   - `policy.py`：验证命令结构化门控——程序白名单（pytest/python/py/ruff/mypy/node/npm/npx/go/cargo/dotnet/mvn/gradle/git），
     `git` 仅限只读子命令（diff/status/show/log/ls-files/check-ignore/blame/rev-parse/describe/branch/tag），
     破坏性 token（rm/mv/sudo/reboot/format/chmod/chown/curl/wget/重定向 `>` `>>` 等）一律拒绝；
     `python -c` 内嵌代码禁止显式文件变更调用（`DESTRUCTIVE_CALLS`）。
   - `runner.py`：受控执行——复用 bash 工具的同一套防护（stdin=DEVNULL 防 Windows 挂起、
     `_wait_with_escalation` 超时+进程树击杀、UTF-8 解码、输出截断 50K），返回结构化
     `VerificationRunResult(exit_code, stdout, timed_out, duration_ms, error)`；
     **执行前第二道闸**：走现有权限引擎（config deny 永远赢，saved 规则不参与）。
   - `evidence.py`：`VerificationRunResult` → `VerificationEvidence`（stdout 截断到 4000 字符尾部，
     超时记 exit 124）。
   - `__init__.py`：`verify_feature_command(feature_id, *, workspace, timeout_s) -> Feature`——
     not_started 自动 claim → policy/权限检查 → 受控执行 → 通过 `harness.features.verify_feature`
     （唯一 passing 入口）更新状态；策略/权限拒绝不抛异常，直接记 failing + 原因、不伪造证据。
   - 存储：feature 状态即证据存储（L2 的 `harness.features`），无需独立 store。
2. **V 系列评测（`evals/cases/verification.py`，零 LLM，真实 subprocess，已注册）**：
   V001 破坏性命令被拒 / V002 确定性命令放行（git 只读）/ V003 成功→passing+证据 /
   V004 失败→failing+last_error / V005 超时杀进程不挂起（2s 超时 <15s 返回）/
   V006 权限 config deny 拦截（未执行、无证据）/ V007 长输出截断 / V008 策略拒绝→failing 无假证据。

**回归结果**：`python -m evals` = **100%（48 pass / 0 fail / 1 warn / 2 skip）**。

**手动演示**：`python demo_l3_verification.py`——真实验证通过→passing、失败→failing、`rm -rf .` 被拒且不执行。

**L3 完成标准自查**：
- [x] 验证命令必须显式声明（feature.verification），非调用时即兴 shell（policy 门控）
- [x] 走现有权限系统：config deny 红线优先（V006）
- [x] 受控执行：超时杀进程（V005）、固定 cwd、stdin 关闭、输出受限（V007）
- [x] 结果记录为证据并更新 feature 状态，passing 只能经此路径（V003/V004）
- [x] 拒绝路径不伪造证据、不污染工作区（V008）

### L4 实施记录（2026-08-05）

**产出**：

1. **`harness/clean/`（新模块）**：
   - `checker.py`：三项确定性检查（零 LLM、零副作用、只报告不自动删除）——
     `temp_artifacts`（硬检查：`*.tmp/*.bak/*.orig/*.swp/*~/.DS_Store/Thumbs.db`，跳过 harness 运行时目录与依赖目录）、
     `feature_state_consistency`（硬检查：passing 但 evidence 为空的 feature = 数据不一致，正常流程不可能产生）、
     `uncommitted_changes`（软信息：git 未提交变更，非 git 仓库跳过，永不阻止）。
   - 三模式：`HARNESS_CLEAN_MODE` 环境变量 `off`（不检查）/ `warn`（默认，报告不阻止）/ `enforce`（硬检查失败阻止）。
   - `CleanReport.hard_failures` = 硬检查失败项；`report.ok` 只由硬检查决定。
2. **接入 `complete_task()`**（`harness/tasks.py`）：函数内延迟 import `harness.clean`（避免循环依赖）；
   enforce 且硬检查失败 → 返回 `Cannot complete ...`，**任务不归档、保持 in_progress**；
   warn 且有问题 → 打印报告照常完成；event-stream 模式下静默（`events.is_enabled()`）。
   未挂普通 Stop hook（遵循计划 §0.3：清洁检查不在每轮触发）。
3. **W 系列评测（`evals/cases/clean.py`，零 LLM，已注册）**：
   W001 off 不检查 / W002 临时工件检出 / W003 干净工作区全过 / W004 enforce 阻止完成（任务保持 active）/
   W005 warn 报告但完成 / W006 passing 无证据检出 / W007 未提交变更是软信息不阻止。

**回归结果**：`python -m evals` = **100%（55 pass / 0 fail / 1 warn / 2 skip）**。

**手动演示**：`python demo_l4_clean.py`——enforce 下残留 junk.tmp 阻止完成（任务仍 active），删除后正常归档。

**L4 完成标准自查**：
- [x] 三模式 off/warn/enforce 齐全，默认 warn（W001/W004/W005）
- [x] 触发点在 complete_task，未挂每轮 Stop hook
- [x] 检查临时工件 + 状态同步（feature/evidence 一致性）（W002/W006）
- [x] 只报告不自动删除、不动用户文件；未提交变更为软信息（W007）
- [x] enforce 失败不归档、状态可恢复（W004）
- [ ] 尚未接入：/check 显式命令、会话退出、worktree 合并触发点（与 L6 /goal 一起留待后续；/goal 已按用户要求暂缓）

### L5 实施记录（2026-08-05）

**产出**：

1. **`config/agents.json` 新增 `evaluator` 角色**：`model_id = mimo-v2.5-pro`（xiaomi-mimo，1M 上下文，与 explore 同模型）；
   工具池**只读**（read_file/glob/bash，无 write/edit）；system prompt 要求只输出 JSON（passed/summary/findings）。
2. **`harness/evaluation/`（新模块）**：
   - `inputs.py`：`collect_inputs(feature_id)` → `EvaluationInputs`（behavior + verification + evidence + git diff + 固定评分标准
     RUBRIC：需求达成度/范围控制/证据一致性/完成声明），`to_text()` 渲染为评估 prompt。
   - `parser.py`：`parse_findings(raw)` 纯函数——从模型文本（可能带 prose/代码围栏）提取首个平衡 JSON 对象，
     校验 passed 为 bool、findings 结构，容错不抛异常（失败记 `passed: None + error`）。
   - `runner.py`：`run_evaluation(feature_id)` —— 校验角色可用 → `run_agent_task(agent_type="evaluator")` →
     `parse_findings` → `harness.features.record_evaluation` 落盘；`requires_evaluation(feature)` 启发式
     （验证命令含 pytest/ruff/mypy/node --test/npm test 等强机器验证则跳过；弱验证且已 claim 才评估，省 token）。
3. **`harness/features` 扩展**：`Feature.evaluation` 字段（默认 None，向后兼容）+ `record_evaluation()` API——
   **advisory 不改状态**（评估不 gate passing；硬门控仍是 L3 机器验证，符合停止判断顺序：机器验证 > feature 状态 > evaluator > agent 自报）。
4. **E 系列评测（`evals/cases/evaluation.py`，零 LLM——模型调用全 mock，已注册）**：
   E001 角色注册+只读工具 / E002 工具池解析无写 handler / E003 输入组装完整 /
   E004 正常 JSON 解析 / E005 乱文本容错 / E006 记录落盘且状态不动 / E007 启发式跳过强验证。

**回归结果**：`python -m evals` = **100%（62 pass / 0 fail / 1 warn / 2 skip）**。

**手动演示**：`python demo_l5_evaluation.py`（mock 模型返回"校验过严"verdict）——弱验证 feature 触发评估、
输入齐全、findings 解析落盘、feature 状态保持 active（advisory）。

**L5 完成标准自查**：
- [x] evaluator 角色只读（E001/E002）
- [x] 输入含原始需求/验收/diff/机器验证结果/评分标准（E003）
- [x] 输出结构化 findings 含证据（E004/E005）
- [x] 简单任务不启用（E007）
- [x] 评估不直接改 feature 状态（E006）
- [ ] 有限返修轮次——留待 L6 /goal（已按用户要求暂缓）

### M008：非交互控制命令（2026-08-05，红→绿）

**缺陷**：`/model`、`/mode`、`/effort` 切换明明是即时配置命令（不需要 LLM 回答），却被当成普通消息处理：
- 后端 `event_stream.py` 主循环对 `user_message` 和 `slash_command` 一视同仁，`running.is_set()` 时全部拒绝并打印 "Agent is already running" → **运行中不能切换模型/mode/挡位**；
- worker 对任何输入（包括会被秒回的 slash 命令）都无条件 `running.set() + emit("agent_start")` → **非交互命令也显示"正在运行"**（spinner 空转）。

**修复**（`harness/event_stream.py` + `node_tui/src/App.tsx`）：
- 新增纯函数 `_is_instant_slash_command(query)`：`/model /effort /mode /models /usage /help` 归类为 instant（纯配置/只读，不碰 context/history/binding）；`/open /resume /clear /rag` 保持走 turn 队列（会改状态，运行中仍拒绝，避免并发）。
- 主循环：instant 命令**跳过 busy 检查**，即时调用 `_handle_slash_command` 并 emit note——不排队、不设 running、不打 "already running"。
- worker：`query` 以 `/` 开头时不 `running.set()`、不 `emit("agent_start")`、不 `emit("agent_end")`——slash 命令不再触发 UI 的 running 状态。
- 前端：`submitPrompt` 对 slash 命令改发 `type: 'slash_command'`（与 `user_message` 区分），后端才能即时处理。

**评测**：新增 M 系列 `m008.instant_slash_command_not_interactive`（零 LLM）：
- instant 分类：`/model xxx`、`/effort high`、`/mode direct` → True；`/open`、`/resume`、`/clear`、普通文本 → False；
- `_handle_slash_command("/effort high", [], None)` 同步返回 note、不抛异常（不发 LLM）。

**回归**：`python -m evals` = **100%（71 pass / 0 fail / 1 warn / 2 skip）**。

**设计原则**：**控制命令 ≠ 交互消息**——UI 的 running 状态只能由"需要 agent 交互的回合"驱动；即时配置切换永远不进入该状态，也不被其阻塞。属于 L0 可观测性/状态机正确性的机制防线。

### 审查修复记录（2026-08-05，代码审查后 8 项修复）

> 触发：对 L2–L5 实现做对抗性代码审查，发现 8 个问题（P0×3 / High×3 / Medium×2），逐项修复并补对抗测试。

| # | 严重度 | 问题 | 修复 |
|---|--------|------|------|
| 1 | P0 | feature 未接入任务完成闭环：registry 无 feature 工具，complete_task 不检查关联 feature | `registry.py` 新增 create_feature/claim_feature/list_features/verify_feature/evaluate_feature 工具 + handler；`complete_task` 增加完成门控：关联 feature 必须全 passing 且未过期（enforce 阻止，warn 报告） |
| 2 | P0 | 验证命令门控可执行任意副作用：`shell=True` + 字符串拼接，`python -c` 任意代码，ask 权限被忽略 | 结构化 argv + `shell=False`；拒绝 shell 元字符（`&& ; | > ` $( 等）；移除 `python -c/-m`；新增独立 `verify_command` 权限域要求显式 allow（ask/deny 均拒绝）；python/node 脚本必须存在于工作区内；验证后检测工作区变更（非只读即失败） |
| 3 | P0 | passing 可伪造且会过期：`.features/` 可被 write_file 直接写；证据不绑定代码快照 | `permissions.json` 对 `.features` 路径 deny（write/edit/bash 全覆盖）；`VerificationEvidence` 增加 `code_snapshot`（git HEAD+工作区指纹）；`feature_is_stale()` 检测 passing 但代码已变；L4 检查纳入 stale + corrupt 文件 |
| 4 | High | evaluator 非真只读（有通用 bash）+ 可能检查错工作区（用冻结 WORKDIR） | agents.json 移除 evaluator 的 bash（只留 read_file/glob）；`run_agent_task` 增加 `cwd` 参数；`run_evaluation` 显式传 feature.workspace |
| 5 | High | evaluator 看不到 untracked 新文件；requires_evaluation 遇 pytest 就跳过关键场景 | `_git_diff` 纳入 untracked 文件全文（最多 50 个、各 8000 字符）；`requires_evaluation` 改为显式 `evaluation_required` 字段（无隐式启发式） |
| 6 | High | L4 检查范围与完成定义不匹配：检查当前活动工作区而非任务 worktree；损坏 JSON 被静默跳过 | `complete_task` 按 `task.worktree` 指定检查根；`list_features` 严格加载 + `corrupt_feature_files()` 报告损坏文件（enforce 硬失败） |
| 7 | Medium | 连续失败验证抛 ValueError（failing→failing 非法） | 状态机允许 `failing→failing` 自环（幂等重试，attempts/evidence 累加） |
| 8 | Medium | 评测污染真实任务归档（TASKS_ARCHIVE_DIR 导入时冻结） | `_archive_dir()` 动态计算；测试与演示随 TASKS_DIR mock 自动隔离归档 |

**新增对抗测试**：V009（工作区变更检测）/ V010（脚本必须存在）/ F008（失败重试幂等）/ F009（stale 检测）/ F010（corrupt 文件）/ W008（完成门控阻止非 passing feature）/ W009（全 passing 放行）/ W010（corrupt 阻断）；V/E 系列同步更新（python -c 移除、evaluator 无 bash、untracked diff、cwd 断言）。

**回归结果**：`python -m evals` = **100%（70 pass / 0 fail / 1 warn / 2 skip）**，修复前漏洞复现全部转为被拒/被降级：
- `python -c "...write_text(...)"` 验证命令 → policy 拒绝，文件未创建
- `write_file .features/feat_x.json` → deny（config deny 优先）
- 任务关联 not_started feature → `Cannot complete`（enforce）
- 验证后改代码 → feature 标记 stale，完成被拒

### L0 验证记录（2026-08-04）

- 端到端跑通：`python -m evals.harness_reliability --task H001 --variant baseline --runs 1`
  → `PASS oracle_passed=True claimed=True rounds=6 tools=7 tokens=611978 23s`
  → baseline completion=100.0%, false_completion=0.0%, overreach=0.0%。
- 踩坑与修复（均已修进代码）：
  1. **Windows stdin 挂起**：shell 工具下 python 因 stdin 未关闭而挂起（CPU 0 无输出）；
     手工命令加 `< NUL`，框架内 subprocess 一律 `stdin=subprocess.DEVNULL`。
  2. **Windows 只读 git 对象**：`shutil.rmtree` 删不掉，需 `_force_rmtree`（先 chmod）。
  3. **oracle 误报越界**：harness 自建 `.project/` 等运行时目录被当成 agent 修改，
     已在 `git_changed_files` 排除 runtime 目录。
  4. **oracle_passed 读取不一致**：`to_dict()`/终端/报告曾读从未赋值的
     `run.oracle_passed` 字段，导致 oracle 通过但报告 FAIL；已统一从 `run.oracle.passed` 推导。
  5. **完成声明误判**：`_claimed_complete` 关键词过窄，补入 "passed"/"通过" 等。
  6. **pytest 冒烟卡住**：根目录管道问题，用 `--tb=short` + 后台 + `< NUL` 解决。
  7. **structured 误报越界**：agent 按配置要求更新 progress.md 被当成越界；
     `allowed_paths` 加入 HARNESS.md / progress.md / feature_list.json。
- 说明：H001 单次运行 token 约 0.6M（含回退重试），跑多配置/多任务时注意预算；
  真实评测建议每次 `--runs 3` 取稳定值，成本约 3×0.6M tokens/任务。

### H001 初步 baseline 数据（2026-08-04，每配置 2 次）

| 配置 | 完成率 | 虚假完成率 | 越界率 | 平均 token | 平均轮数 | 平均工具 |
|---|---|---|---|---|---|---|
| baseline | 100% | 0% | 0% | ~254K | ~7 | ~8 |
| structured | 100% | 0% | 0% | ~64K | ~9 | ~12 |

初步观察（样本小，仅作方向参考）：
- **structured 的 token 稳定只有 baseline 的 ~1/4**（两轮均 64K vs 254K），
  提示「仓库即规范 + 进度/功能清单」显著减少探索与走弯路成本。
- 完成率均为 100%，**H001 对当前模型过简单、区分度不足**；后续应优先建
  H002–H005（跨模块 / 过度扩展陷阱 / 跨会话 / 提前完成陷阱）来测完成率差异。
- 下一步建议：先建 H002–H005 fixture，再对全任务集跑 baseline vs structured 各 3 次，
  形成可下结论的对照数据。

### H002–H005 任务已建（2026-08-04）

| ID | 名称 | fixture | 隐藏检查脚本 | 测什么 |
|----|------|---------|--------------|--------|
| H002 | 跨模块用户偏好 | `preferences/`（app/store/api 三层） | `checks/h002_pref_check.py` | 跨模块理解 + 401/404/读写（L10） |
| H003 | 邮箱校验（过度扩展陷阱） | `email_validation/` | `checks/h003_email_check.py` | 是否顺手改无关代码（L07） |
| H004 | 双会话任务清单 | `task_store/`（骨架） | `checks/h004_task_check.py` | 跨会话恢复、是否重复实现（L05/L12） |
| H005 | 密码重置（提前完成陷阱） | `password_reset/` | `checks/h005_reset_check.py` | 单测通过但完整链路缺失（L09/L10） |

实现要点：
- 每个任务有独立 **hidden check script**（`evals/harness_reliability/checks/`），
  oracle 在 agent 跑完后用 `python <script> <workspace>` 独立验收，agent 不可见。
- **H004 跨会话**：runner 支持 `requires_multi_session`，同一 workspace 连跑两段
  独立 agent_loop（第二段全新 messages，模拟新会话；structured 靠 progress.md 接力）。
- 已用「未修复 fixture → oracle 判失败；正确修复 → oracle 判通过」双向冒烟
  验证全部 5 个任务的检查有效性（无 LLM，不烧 token）。
- H003 设计教训：fixture 自带测试不能锁定「弱行为」（原 `validate_email("a@b") is True`
  与正确实现矛盾），已改为中性测试。
- oracle 范围检查支持**目录前缀**匹配（`"tests/"` 匹配 `tests/test_app.py`），
  避免「Add tests」任务被误判越界。

### 全任务集真实 LLM 冒烟（2026-08-04）

- H001 × baseline/structured：两配置均 PASS，structured token ≈ 64K vs baseline ≈ 254K。
- H003 × baseline/structured：两配置均 PASS（oracle 通过、无越界误报），
  structured token ≈ 133K vs baseline ≈ 315K（~2.4x 差距，样本各 1）。
- 结论：评测链路对 5 个任务全部可用（未修复判失败 / 修复判通过 / 真实 agent 可跑通）；
  下一步可对全任务集跑 baseline vs structured 各 3 次收正式对照数据。

### 全任务集 30 次正式对照（2026-08-04，重评分后）

- 运行：5 任务 × 2 配置 × 3 次 = 30 次，全部 oracle 通过（completion=100%），
  虚假完成率 0%、越界率 0%。
- **关键教训 1：H002 检查脚本误杀**——agent 用了 `handle_get_user_preferences` /
  `handle_set_preferences` 等合理命名，检查脚本只认 `handle_get_preferences`；
  且返回结构可能是裸 dict 或 `{"preferences": ...}`。已修脚本（探测多命名 +
  兼容两种返回结构），重评分后 6/6 全过。教训：**检查脚本必须与任务描述松耦合，
  只验证行为契约，不锁死命名/结构**。
- **关键教训 2：当前任务对 deepseek-v4-flash 区分度不足**——所有任务裸跑（baseline）
  也能 100% 做对，测不出 harness 价值。token 差异存在但被单次方差淹没。
- 决定：加更难任务 H006（模糊需求）/ H007（多文件重构），并进入 L1 路由页，
  把评测框架作为「回归防线」（确保 harness 改动不破坏基本能力）。

### H006 / H007 已建（2026-08-04，区分度任务）

| ID | 名称 | fixture | 隐藏检查 | 测什么 |
|----|------|---------|----------|--------|
| H006 | 模糊搜索需求 | `search_service/`（契约在 README） | `h006_search_check.py` | 主动读文档 vs 瞎猜；名称/类别、大小写、上限 10、空查询（L03 仓库即规范） |
| H007 | 多文件配置重构 | `refactor_service/`（3 模块硬编码） | `h007_refactor_check.py` | 跨文件一致性、是否改一半（L07 WIP / L10 端到端） |

- 双向冒烟通过：未修复 → oracle 失败；正确修复 → oracle 通过。
- H006 真实 LLM 冒烟：agent 主动读 README 找到契约，PASS（39s, 313K tokens）。


---

## 7. M 系列：机制回归评测（2026-08-05，零 LLM 成本）

审查「全局/局部协同」时发现的关键缺陷，已转化为 M 系列机制评测（`evals/cases/mechanisms.py`），
全部零成本确定性断言，可进 CI（`python -m evals`）：

| ID | 断言 | 对应缺陷 | 状态 |
|----|------|----------|------|
| M001 | bash 跑 python 不挂起（stdin=DEVNULL） | bash 子进程继承 TUI stdin 管道 → python 等输入卡死 | PASS（修复后） |
| M002 | session context 注入 OS/shell 提示 | 平台提示缺失导致 agent 先踩 ls/grep 坑 | PASS |
| M003 | subagent bash 工具定义带 shell 语义 | subagent/teammate 只看到 "Run a shell command" | PASS |
| M004 | 项目 HARNESS.md 可发现且可注入 | 路由页未生效 | PASS |
| M005 | bash 在活动工作区执行（跟随 /open） | 切换工作区后命令跑错目录 | PASS |

实现要点：
- M001 用子进程 probe 跑真 run_bash（规避评测进程内存里的旧代码），断言 python -c 0.2s 返回。
- 踩坑：probe 生成命令字符串用 json.dumps 会转义引号 → 命令被包多余引号 → cmd 解析错
  （报 The system cannot find the path specified.）；改用 repr() 生成 Python 字面量修复。
- 修 M001 时同时发现既有技术债：perm.bash_confirm_* / perm.mcp_destructive 引用已删除的
  harness.hooks.ask_allow（3 个 FAIL，非本轮引入，待修）。

### 全局/局部协同设计（审查结论，待实施）

分三层信息源，主 Agent / subagent / teammate 全部覆盖：

```text
Runtime facts（代码生成）   OS/shell、权限、工具语义 → 工具 schema + static 短提示
Global handbook（用户级）   ~/.harness/HARNESS.md：长期偏好、默认纪律
Project handbook（项目级）  <workspace>/HARNESS.md：命令、结构、约束、路由
```

优先级：运行时安全/工具事实 > 当前明确要求 > 项目说明 > 全局默认。
待办：TUI /open 清空旧 history + 重载新项目 HARNESS.md（M 系列已覆盖的回归点）；
bash 工具描述统一注入 shell 语义到 subagent/teammate（M003 已覆盖检查点）。


### M006：TUI /open 重载项目说明（2026-08-05，红→绿）

缺陷：event-stream 后端的 `/open` 切换工作区后只 `update_context(context, history)`，
没有清空旧 history、没有重新加载新项目的 HARNESS.md → agent 继续遵守旧项目规则。

- 复现测试先写（红）：`tests/test_event_stream_open.py`，断言 /open 到 B 后
  context 含 `pytest b`、不含 `pytest a`、history 清空。修复前失败信息精确显示
  `'# ProjectA ... pytest a'` 泄漏。
- 修复（绿）：`harness/event_stream.py` 的 /open 分支对齐 CLI 路径——
  `history.clear()` + `context = update_context({}, [])` +
  `apply_project_instructions(context, start=Path(target))` + `reset_ephemeral_cache()`；
  补 `from pathlib import Path`。
- 踩坑：Windows 下不能删除「当前 cwd」的目录（PermissionError）→ 测试 finally
  `os.chdir(original_cwd)` 再清理临时目录。
- 已注册为常驻机制评测 `m006.open_reloads_project_md`（`evals/cases/mechanisms.py`）。
- mini-eval score：87% → 91%（M 系列 6/6 全过；剩余 3 FAIL 为既有 perm.ask_allow 技术债）。


### 权限体系修复：saved allow 不能覆盖 config deny（2026-08-05）

背景：修 `perm.bash_confirm_*` / `perm.mcp_destructive` 3 个既有 FAIL 时，
发现更深层问题——持久化规则里有一条历史遗留的 `bash:* allow`（某次「always」误点），
导致**所有 bash 命令被永久放行**，`del *: ask`、`rm *: deny` 等配置规则全部失效。

修复（`harness/permissions/engine.py`）：
- `evaluate_single_permission` 先查 config：若 config 明确 `deny`（如 `rm *`、`sudo *`），
  直接拒绝，**saved allow 不能覆盖 deny 红线**。
- saved rule 仍可覆盖 config 的 ask（用户显式授权），但不能覆盖 deny。
- 清理历史遗留的 `bash:* allow`（保留 201 条精确规则），恢复 `del *: ask` 语义。

验证：
- `del ./tmp_build` → ask（恢复配置语义）；`rm ./old.txt` / `sudo reboot` → deny。
- `python -m evals` score 从 91% → **100%**（32 pass / 0 fail；warn=1 为已知
  plan 模式 MCP 未 gate 的 gap；skip=2 为 live 需 API）。

评测测试同步修正（`evals/cases/permissions.py`）：
- mock 目标从已删除的 `harness.hooks.ask_allow` 改为 `harness.hooks.ask_permission`
  返回 `PermissionResponse`（当前真实确认机制）。
- `rm` 在配置里是 deny（不触发 ask），改用 `del *`（ask）测确认路径；
  MCP 测试改为断言当前真实行为（`mcp__*: allow` 下 destructive 也 allow）。


### M 系列评测数据统计（2026-08-05 最新）

**当前状态：`python -m evals` = 100%（33 pass / 0 fail / 1 warn / 2 skip）**

M 系列 7 项逐项数据（latest.json, 2026-08-05T06:21:54Z）：

| ID | 名称 | 状态 | 耗时 | 覆盖的缺陷/修复 |
|----|------|------|------|------------------|
| M001 | bash 跑 python 不挂起 | PASS | 575ms | stdin 继承 → 挂起（已修） |
| M002 | 平台提示注入 | PASS | 12ms | agent 先用 ls/grep 踩坑（已修） |
| M003 | subagent 工具带 shell 语义 | PASS | <1ms | 子 agent 无环境提示（已确认） |
| M004 | HARNESS.md 可注入 | PASS | <1ms | 路由页未生效（已确认） |
| M005 | bash 跟随工作区 | PASS | 218ms | 切换后命令跑错目录（已确认） |
| M006 | /open 重载项目说明 | PASS | 17ms | 旧项目规则泄漏（已修） |
| M007 | saved allow 不能覆盖 deny | PASS | <1ms | 安全漏洞：bash:* allow 关掉 deny-list（已修） |
| M008 | 非交互控制命令不触发 running | PASS | <1ms | /model /mode /effort 运行中可切换、不显示"正在运行"（已修） |

**总分 evolution（今天 6 次运行）**：
- 87.1% → 90.3% → 87.5% → 90.6% → 93.8% → 96.9% → **100%**（7 次，含 M 系列添加与权限修复）

**M 系列统计小结**：
- 7 项全过，总耗时约 0.82s（几乎全零成本）
- 3 项是「已修缺陷的回归防线」（M001/M006/M007，红→绿）
- 4 项是「现有机制的正确性确认」（M002-M005）
- 全部零 LLM 成本，可常驻 CI
- 防止的回归：bash 挂起 / 平台提示丢失 / 子 agent 命令错误 / 路由页失效 /
  工作区错乱 / 项目规则泄漏 / 权限绕过


### H 系列成本与行为统计（2026-08-05，30 次运行重评分后）

**总体（5 任务合并，每配置 15 次）：**

| 配置 | 平均 token | 平均时间 | 平均工具调用 | 平均回合 | 完成率 |
|------|-----------|---------|-------------|---------|--------|
| baseline | 197,962 | 59.6s | 14.7 | 12.3 | 100% |
| structured | 187,110 | 78.2s | 24.2 | 17.2 | 100% |
| 变化 | **-5.5%** | **+31.2%** | **+65.0%** | **+39.5%** | 持平 |

**分任务（structured vs baseline）：**

| 任务 | baseline token | structured token | 变化 | 工具调用变化 | 时间变化 |
|------|---------------|-----------------|------|-------------|---------|
| H001 | 136K | 70K | **-48%** | 9→14 | 42s→55s |
| H002 | 232K | 204K | -12% | 18→20 | 98s→72s |
| H003 | 60K | 206K | **+248%** | 9→21 | 35s→68s |
| H004 | 374K | 272K | -27% | 23→40 | 79s→116s |
| H005 | 188K | 183K | -3% | 14→25 | 44s→80s |

**关键结论（修正此前误判）：**

1. **「structured 省 4 倍 token」是单次观测偏差，全量统计不支持**。
   30 次平均只省 5.5%，且因任务而异：H001/H002/H004 省（少探索），
   H003/H005 反而多（structured 要求读 HARNESS.md/progress/feature_list、
   更新 progress.md、多轮验证，产生额外工具调用）。

2. **structured 的真实代价是「操作开销」**：工具调用 +65%、回合 +39.5%、
   时间 +31.2%。这些是读/写状态文件、跑验证的固定成本。

3. **完成率两种配置都是 100%（区分度不足）**——H001-H007 对当前模型
   仍偏简单，token/时间差异比完成率差异更可测。

4. **统计方法教训**：单次/少量 run 的 token 对比方差极大（同一任务
   baseline 曾出现 52K 和 379K），必须 ≥3 次取均值才能下结论；
   结论要按任务拆分，不能只看合并平均。


### M 系列变化统计（2026-08-05，跨 10 次运行）

M 系列是零 LLM token 的确定性断言，其「变化」体现在**通过状态 + 耗时**：

| 运行 | M001 | M002 | M003 | M004 | M005 | M006 | M007 |
|------|------|------|------|------|------|------|------|
| 05:04:59 | F/565ms | P/11ms | P/0ms | P/0ms | P/217ms | — | — |
| 05:01:14 | F/563ms | P/11ms | P/0ms | P/0ms | P/215ms | — | — |
| 05:03:37 | F/586ms | P/12ms | P/0ms | P/0ms | P/218ms | — | — |
| 05:06:39 | P/570ms | P/12ms | P/0ms | P/0ms | P/217ms | — | — |
| 05:37:56 | P/868ms | P/16ms | P/0ms | P/1ms | P/218ms | F/222ms | — |
| 05:40:51 | P/607ms | P/13ms | P/0ms | P/0ms | P/218ms | P/17ms | — |
| 06:06:15 | P/608ms | P/13ms | P/0ms | P/0ms | P/217ms | P/18ms | — |
| 06:08:11 | P/595ms | P/13ms | P/0ms | P/0ms | P/216ms | P/16ms | — |
| 06:14:13 | P/576ms | P/12ms | P/0ms | P/0ms | P/218ms | P/20ms | — |
| 06:21:54 | P/575ms | P/12ms | P/0ms | P/0ms | P/218ms | P/17ms | P/0ms |

**变化解读：**

1. **M001 红→绿**：前 3 次 FAIL 是评测自身 bug（probe 引号转义），修复后稳定 PASS，
   耗时稳定在 ~575-868ms（子进程启动 + 真 bash 调用）。
2. **M006 红→绿**：出现即 FAIL（旧项目规则泄漏，222ms），修复后稳定 PASS（16-20ms）。
3. **M007 最后加入**：0ms（纯内存断言），作为安全修复的回归防线。
4. **M002-M005 从未 FAIL**：机制确认项，耗时稳定（12-218ms）。
5. **总成本恒定**：7 项 ~822ms、0 token；对比 H 系列单次 ~19 万 token / ~60s——
   M 系列适合常驻 CI，H 系列按需真跑。

**两类评测成本对比（一次完整回归）：**

| 维度 | M 系列（机制） | H 系列（任务） |
|------|--------------|--------------|
| LLM token | 0 | ~19 万/次运行 |
| 耗时 | ~0.8s | ~60s/次 |
| 判据 | 确定性断言 | oracle 行为验收 |
| 适合 | CI 常驻 | 修复前后对比 |


### 权限规则审计（2026-08-05，E 项）

清理 `bash:* allow` 后，对全部权限状态做彻底审计：

| 范围 | 数量 | 过宽项 |
|------|------|--------|
| saved persistent rules | 201 | **0**（全部是精确 URL / 文件路径 / 具体命令） |
| session rules | 0 | — |
| config 顶层 | — | `bash` 有 `*: ask` + 具体规则（`del *: ask`、`rm *: deny`…）；`mcp__*: allow` 为已知设计选择 |

结论：无其他需要清理的过宽规则，权限体系当前干净。
