# 002 — API 缓存命中率优化（与 001 Todo 防偏移的权衡）

**状态：** 静动态分层 + append-only 历史 + 检查点压缩已落地（2026-07）
**关联：** [001 偏移](./001-todo-drift.md) · [004 压缩](./004-context-compaction.md)（时间分钟级利于 ephemeral 缓存）  
**日期：** 2026-07

---

## 一、问题是什么

### 1.1 账单现象

DeepSeek 控制台一度出现：

| 指标 | 数值 | 含义 |
|------|------|------|
| 缓存未命中 | **~134 万 token** | 按 **全价输入** 计费（累加） |
| 缓存命中 | **0** | 几乎没有走廉价缓存 |
| 输出 | **~6 万 token** | 模型生成的内容（与 prompt 缓存无关） |

> **术语：** 「未命中」越多越贵；「命中」越多越省。  
> 命中 0 + 未命中 134 万 = **命中率极低**，不是「未命中很少」。

### 1.2 和 001 的关系

[001](./001-todo-drift.md) 为防任务偏移，把 **todos 全表写进 system**。用户担心：

> 防偏移和省钱是不是二选一？

**结论：不是。** 问题是 **动态内容放错了层**（塞进 system 头部），不是「不该让模型看见 todos」。

### 1.3 改完 Phase 1 后的中间态

静态 system 拆出来后，真实 agent 命中率从 **~0%** 提到约 **~40%**（仍不理想）。  
经实验与第二轮优化（`if_unchanged` + 正确读 usage 字段），**受控 live 测试可达 ~97%**——说明方向对，但实验条件比真实长任务更理想（见第六节）。

---

## 二、缓存怎么计费（科普）

### 2.1 这是什么

| 是 | 不是 |
|----|------|
| 云厂商对 **输入 prompt 前缀** 的 KV / 硬盘缓存 | Python `_clients` 连接池、harness 本地缓存 |
| DeepSeek **自动**前缀匹配，无需 `cache_control` | 应用层语义缓存 |
| 按 token 计费：命中价 ≈ 未命中的 **1/50～1/10** | 输出 token 的缓存 |

一次请求输入大致为：

```text
tools[]  →  tool schema（常很大）
system   →  系统指令
messages →  历史 + tool 结果 + 用户话
```

**从第 1 个 token 起**与上次请求比对：**连续相同的前缀 = hit（便宜）**，其余 = **miss（全价）**。

### 2.2 DeepSeek 返回字段（重要）

经 live 实验确认，DeepSeek Anthropic 兼容接口用的是：

| 字段 | 含义 |
|------|------|
| `cache_read_input_tokens` | **命中**（从缓存读取） |
| `input_tokens` | **未命中**（本轮新算的输入尾巴） |
| `output_tokens` | 输出（与缓存无关） |

**不是**账单页可能写的 `prompt_cache_hit_tokens`（部分控制台汇总用另一套名）。  
代码统一在 `harness/usage.py` 的 `parse_cache_usage()` 解析；`llm.py` 打印 `[cache]` 行。

### 2.3 为什么 Agent 特别容易「未命中爆炸」

```text
用户 1 句话
  → LLM → tool → LLM → tool → …（常 20～30 次 API）
```

每轮输入 = system + tools + **越来越长的 messages**。若 **system 每秒变**，几乎 **整段都算 miss**：

```text
未命中累加 ≈ 单次 miss × 调用次数 × 会话数 → 百万 token 很正常
```

输出只有几万 token 也正常——大钱在 **反复读入的长上下文**。

### 2.4 为什么别人家历史变长，命中反而升高

缓存比的是 **前缀有多长和上次完全一样**，不是「整段有没有变」。

```text
         ←—— hit（与上次相同）——→← miss（新尾巴）→
请求 N:   [tools][system][msg…msg] [新 tool 结果 + session 块]
请求 N+1: [tools][system][msg…msg][asst][tool] [新内容]
          └──── 与请求 N 相同的部分 ────┘
```

成熟 Agent（Cursor / Claude Code / Codex）：

- **冻结** system + tools
- **动态**内容（todos、时间、打开文件）追加在 **messages 尾部**
- 历史越长 → 可复用前缀越长 → **hit 应升高**

我们改前的问题：**最前面**每秒在变（`Current time` 在 system 里），历史越长越亏。

---

## 三、根因：我们 harness 里谁在破坏前缀

### 3.1 破坏源排序

| 破坏源 | 来源 | 影响 |
|--------|------|------|
| `Current time: datetime.now()` | s20 模板 | **极大**：每秒改 system 开头 |
| todos / model / mode 在 system | 001 + 多模型功能 | 大：一改就断前缀 |
| 每轮完整 ephemeral（秒级时间） | Phase 1 初版 | 中：增加每轮 miss 尾巴 |
| 大段 tool 结果首次进入 | agent 常态 | 新尾巴算 miss；必须在入历史前限制 |
| 逐轮 micro/snip 改写旧历史 | 旧压缩链 | **极大**：历史中段改变，后续前缀全部失效 |
| 检查点 compact / 换模型 | 长会话 | 前缀作废，但只产生一次可解释冷启动 |

### 3.2 system 里那些动态块从哪来

| 块 | 何时加入 | 根据什么 |
|----|----------|----------|
| identity / tools / workspace / skills | Initial（s20 拆包） | `learn-claude-code` 教程综合章 |
| `Current time` | Initial | **直接抄 s20**；为 cron/时间感知，未考虑缓存 |
| memories / MCP / teammates | Initial | s09 / s19 / s15–17 教学：live context 进 system |
| `Current model` | commit `ad0fc9d` | `/model` 多厂商，让模型知道当前端点 |
| project / RAG 说明 | 后续 commit | 论文仿写、resume |
| todos 全表 | 001 对齐 Claude Code | 防偏移；当时放在 system（后修正） |
| mode 段 | 未提交 orchestrate | direct / plan / orchestrate |

设计意图一直是 s20 注释：**每轮用 live context 重建 system**。  
**没有**按「提高 cache hit」设计；也 **没有**采用 Claude Code 的「system 冻结 + 动态进 messages」。

---

## 四、解决思路（讨论结论）

### 4.1 核心原则

| 目标 | 做法 |
|------|------|
| 任务不偏移（001） | 模型仍要看 **完整 todos** + reminder + UI |
| 缓存命中 | **稳定内容在前**，**易变内容在 messages 尾部** |

**不是二选一，是分层。**

### 4.2 目标请求结构

```text
API 请求
├── tools[]                         # 稳定，可缓存（我们约 ~3200 token）
├── system                          # 仅静态：identity / tools 说明 / skills
└── messages[]
    ├── … 持久化历史 …
    └── <session-context>（可选）    # 时间 / model / mode / todos；API 专用，不落盘
```

借鉴 Claude Code / Anthropic：

- 不要把 `Current time`、todos 放在 **cache marker / 前缀最前段**
- 动态块用 **ephemeral user 消息**，`serialize_messages` 时剥掉

### 4.3 策略对比（实验验证）

| 策略 | 做法 | 模拟 ~8 轮 | live ~5 轮 |
|------|------|------------|------------|
| `legacy_system` | 动态全在 system | ~78%（模拟偏乐观） | 不推荐 |
| `current` | 每轮发完整 ephemeral | ~75% | — |
| **`ephemeral_if_unchanged`** | 会话块未变则跳过 | **~80%** | **~97%** |
| `slim_context` | 去掉 time/mode/mcp 等 | ~78% | 可选后续 |
| `time_minute` / `time_none` | 降低时间粒度 | 与 current 接近 | 次要 |

**线上默认：`always`。** 真实 API 请求不会保留上一轮 ephemeral 消息；直接跳过未变化的块会使模型丢失 mode、项目规则和 todos。`ephemeral_if_unchanged` 仅保留为离线缓存实验。

---

## 五、我们做了什么（实施清单）

### 5.1 Phase 1 — 拆分静态 / 动态

| 改动 | 文件 |
|------|------|
| 拆 `harness/prompts/` 包 | `static.py` / `dynamic.py` / `ephemeral.py` |
| LLM 只发静态 system | `loop.py` → `assemble_static_system_prompt()` |
| 动态块作 API 尾注 | `messages_with_ephemeral_context()`，标记 `<session-context>` |
| 不落盘 | `session.serialize_messages` 跳过 ephemeral |
| 新 user turn 重置 | `agent_loop` 开头 `reset_ephemeral_cache()` |
| todo 更新后强制注入 | `todo_write` 后 `reset_ephemeral_cache()` |

### 5.2 Phase 1b — 可观测 + 实验

| 改动 | 文件 |
|------|------|
| 统一 usage 解析 | `harness/usage.py`（`cache_read_input_tokens` 等） |
| 终端日志 | `llm.py` → `[cache] hit=… miss=… (%)` |
| 离线模拟多策略 | `harness/prompts/cache_experiment.py` |
| CLI | `scripts/run_cache_experiment.py` |
| 单元测试 | `tests/test_cache_experiment.py`、`tests/test_usage.py` |

### 5.3 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `HARNESS_EPHEMERAL_POLICY` | `always` | 每轮发送当前 session 块，保证无状态 API 请求不会丢失 mode、项目规则和 todos |
| `ephemeral_if_unchanged` | 仅离线实验 | 会话块未变则跳过，不能直接用于真实 agent loop |

### 5.4 Phase 2 — append-only 历史

进一步分析真实 DeepSeek 长任务后发现：静态 system 已稳定，但旧
`prepare_context()` 每轮执行 `tool_result_budget → snip_compact → micro_compact`，
会渐进改写已经发过的 tool result。精确前缀缓存因此仍会在工具循环中反复断裂。

现改为：

1. `build_user_content()` 在 tool result 首次追加前统一调用 `stabilize_tool_results()`；
2. 单结果默认最多 `12000` 字符，并行轮共享 `40000` 字符；
3. 超限全文落盘，模型只收到一次定型的头尾 preview + `read_file` 路径；
4. 未达到上下文阈值时，`prepare_context()` 不修改历史；
5. 只在主动 token 阈值或 API prompt-too-long 时做一次检查点压缩。

相关配置：

```bash
HARNESS_TOOL_RESULT_MAX_CHARS=12000
HARNESS_TOOL_ROUND_MAX_CHARS=40000
HARNESS_AUTOCOMPACT_PCT=83.5
```

预期曲线从“旧结果不断变化、连续 miss”变为“每轮只新增尾巴、hit 前缀持续增长；
检查点处冷启动一次，然后重新增长”。

---

## 六、实验结果与怎么解读

### 6.1 离线模拟（不耗 API）

```bash
python scripts/run_cache_experiment.py --rounds 8
python -m unittest tests.test_cache_experiment tests.test_usage -v
```

典型汇总：

```text
ephemeral_if_unchanged  79.8%   total_hit 17078   total_miss 4328
current                 75.2%   total_hit 17097   total_miss 5631
```

`if_unchanged` 在第 2、3、5–8 轮 **ephemeral=0**（跳过重复 session 块），miss 仅 ~360/轮（主要是新 tool 结果）。

### 6.2 Live API（DeepSeek `deepseek-v4-flash`）

```bash
python scripts/run_cache_experiment.py --live --rounds 7 --result-chars 20000
```

Phase 1 静动态分层实测（2026-07）：

```text
round 1: api hit=3200  miss=71   (~98%)
round 2: api hit=3328  miss=116  (~97%)
round 3: api hit=3584  miss=33    (~99%)
round 4: api hit=3712  miss=78    (~98%)
round 5: api hit=3840  miss=123   (~97%)

Live totals: hit=17664  miss=421  rate=97.7%
```

Phase 2 append-only 的旧/新历史 A/B 结果见 [013](./013-append-only-history-cache.md)：warm hit rate 从 **26.3%** 提升到 **76.8%**，warm miss token 减少 **74.6%**。

**解读：**

- **round 1 就有 hit≈3200** → tools + static system 立刻走缓存（服务端可能已有热前缀）
- **hit 随轮次增长**（3200→3840）→ 共享 history 前缀在变长 ✅
- **miss 仅几十～一百多/轮** → 新尾巴很小 ✅

### 6.3 为什么实验 ~97%，真实 agent 可能只有 ~40%～70%

| 因素 | live 实验 | 真实 `main.py` agent |
|------|-----------|----------------------|
| 轮数 | 5 轮 | 常 20～30+ |
| tool 结果 | 短文本 filler | `read_file` / `rag_search` 可能很大 |
| compact / 换模型 | 无 | 有，前缀作废 |
| session 块 | `if_unchanged` 跳过 | 已默认启用 |
| 累计账单 | 单次短实验 | 多任务长期累加 |

**实验高 = 证明策略有效（上限）**；**账单整体**仍取决于任务长度与工具输出体积。  
从 **0% → 40%** 是第一大步；**40% → 60%+** 靠 `if_unchanged` 与稳定 system 在长跑中生效。

### 6.4 修复 live 实验踩过的坑（备忘）

1. **伪造 `tool_result` 无对应 `tool_use`** → API 400；改为合法文本 history 增长  
2. **只读 `prompt_cache_hit_tokens`** → DeepSeek 返回 0/0；改读 `cache_read_input_tokens` + `input_tokens`  
3. **每轮 `reset_ephemeral_cache()`** → 破坏 `if_unchanged`；仅在新 user turn / `todo_write` 时 reset

---

## 七、和 001 的衔接（不能回退什么）

| 若为了缓存回退 | 后果 |
|----------------|------|
| todos 不注入模型 | 任务偏移复发 |
| 只靠空 reminder | 长上下文猜错表 |
| 只靠终端 UI | 模型看不见 |

**保留：** 完整 todos、Rich Tasks、`format_todo_reminder()`、tool schema 纪律。  
**只改：** 注入层从 system → ephemeral messages + `if_unchanged`。

---

## 八、如何验证改动生效

### 8.1 跑 agent 时看终端

每次 LLM 后应有：

```text
  [cache] hit=3328 miss=116 (97%) out=…
```

健康信号：

- 第 2 次 tool 回环起 **hit > 0**
- 同一会话、同模型，hit **缓慢上升**
- 不是长期 `hit=0`

### 8.2 跑实验脚本

```bash
# 离线对比策略
python scripts/run_cache_experiment.py --rounds 8

# 真 API：旧版渐进改写 vs 新版 append-only
python scripts/run_cache_experiment.py --live --rounds 7 --result-chars 20000
```

### 8.3 验收标准

1. **防偏移**：001 四条验收仍通过  
2. **缓存**：同轮 5+ 次 LLM，第 2 次起 `cache_read` / hit **> 0**  
3. **账单**：长任务后未命中增速 **低于改前**（尤其无「system 每秒变」时期）

---

## 九、仍观察（有痛点再商量）

| 项 | 说明 |
|----|------|
| Anthropic `cache_control` | 显式 system block 缓存标记（DeepSeek 自动前缀已够用时可不做） |
| 更瘦的 ephemeral | 仅在 todo 变更 / reminder 时注入 |
| 运维习惯 | 同会话少 `/model`、少无触发整段 compact（会打断前缀） |
| 004 时间粒度 | 已默认分钟；`HARNESS_TIME_GRANULARITY=seconds` 可恢复秒级 |

---

## 十、相关文件

| 路径 | 作用 |
|------|------|
| `harness/prompts/static.py` | 稳定 system |
| `harness/prompts/dynamic.py` | 会话状态正文 |
| `harness/prompts/ephemeral.py` | API 尾注 + `if_unchanged` |
| `harness/prompts/cache_experiment.py` | 策略模拟 |
| `harness/usage/` | usage 字段归一化 + 本地日流水 `/usage` |
| `harness/loop.py` | 调用与 reset 钩子 |
| `harness/llm.py` | `[cache]` 日志 |
| `harness/project/session.py` | 剥 ephemeral 不落盘 |
| `scripts/run_cache_experiment.py` | 离线 prompt 分层模拟；`--live` 运行历史改写 A/B |
| `tests/test_cache_experiment.py` | 离线断言 |
| `tests/test_usage.py` | usage 解析测试 |

---

## 十一、参考

- [DeepSeek — 上下文硬盘缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)
- [DeepSeek — 缓存公告与定价](https://api-docs.deepseek.com/zh-cn/news/news0802)
- [Anthropic Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [anthropics/skills — prompt-caching.md](https://github.com/anthropics/skills/blob/main/skills/claude-api/shared/prompt-caching.md)
- 本仓库 [001-todo-drift.md](./001-todo-drift.md)

---

## 十二、时间线（简）

| 阶段 | 事件 |
|------|------|
| 发现 | 账单未命中 ~134 万、命中 0 |
| 分析 | system 内 `Current time` + 动态块破坏前缀；与 001 todos 张力 |
| Phase 1 | 拆 static / ephemeral；`[cache]` 日志 |
| 中间 | 真实 agent ~40% 命中 |
| 实验 | 模拟多策略；live 修 400 + usage 字段 |
| 默认策略 | `if_unchanged`；live **~97%**（受控条件） |
| 004 | 动态时间默认分钟，减少 ephemeral 每秒抖动 |
| 文档 | 本文 Consolidated；总览见 [bugs README](./README.md) |
