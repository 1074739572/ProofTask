# Classic 页面展示与 Agent 失败恢复设计

## 用户反馈

1. Classic 屏幕输出难看：手工框线不对齐，中文/ANSI 宽度混乱，统计框尤其明显。
2. 有时重复打印用户命令：输入后又以提示符样式回显，滚动历史显得重复。
3. 分发子 Agent 时不可见：只看到 `Worker running tool`，不知道子 Agent 当前阶段、目标、产出或失败原因。
4. 主 Agent 与子 Agent 都失败时可能没有有用回复：例如 DeepSeek Pro 余额不足后，只留下 API 错误或 inbox 片段，缺少兜底总结与可恢复模型建议。

## 设计原则

- Classic 不是全屏 TUI：保留滚动式输出，不接管鼠标。
- 页面不要手写复杂框线：优先使用 Rich `Panel` / `Table` / `Group`，无 Rich 时退化为纯文本列表。
- 用户命令只出现一次：输入行本身已显示用户输入，正常轮次不再额外打印 `> 用户命令`；需要记录时只打印简短阶段。
- 子 Agent 输出要“看得懂”：每个子 Agent 有生命周期状态，而不是一堆孤立工具调用。
- 错误要能收口：不管主模型、子模型还是 fallback 模型失败，都要返回一段面向用户的错误总结和下一步。

## Classic 页面布局

### 正常回合

```text
[thinking] 我将先读取 docs 并整理差异
  tool read_file docs/bugs/README.md -> ok
  tool bash git diff --stat -> ok

回答正文...

Changed files:
  · docs/classic-agent-display-recovery-design.md

────────────────────────────────────────────────────────
model deepseek-v4-flash · mode direct · ctx 12% · cache 96% · mcp 3
```

### 子 Agent 回合

```text
Agents
  route_explore_17783  running  探查 classic/TUI 差异
    1. started
    2. reading files: harness/ui/tui/app.py
    3. found: TUI has inline picker + metrics
  deepseek-pro-impl    failed   余额不足，未完成计划

兜底总结：
- MiMo 已完成只读差异探查；DeepSeek Pro 因余额不足失败。
- 已掌握足够信息：classic 缺 resume picker、稳定 stats、底栏与子 Agent 状态展示。
- 可以改用可用模型继续，或由当前主 Agent 直接实现。
```

### 错误回合

```text
模型调用失败
- 当前模型：deepseek-v4-pro
- 错误：INSUFFICIENT_BALANCE / 403
- 已尝试：deepseek-v4-flash, glm-5.2-flash
- 结果：fallback 也不可用
- 下一步：充值/切换 /model，或设置 HARNESS_RECOVERY_MODELS
```

## 子 Agent 状态规范

子 Agent progress message 不再只有 `Worker running bash`，而改成结构化短句：

- `started: <任务摘要>`
- `thinking: planning next step`
- `reading: <path>`
- `running: <tool> <short input>`
- `waiting: plan approval <request_id>`
- `found: <一句发现>`
- `failed: <错误摘要>`
- `done: <一句结果>`

Classic 显示最近 N 条 progress，TUI 可继续使用后台托盘或 Chat 系统消息。

## 恢复策略

### 错误分类

- 可重试：429、502、503、529、timeout、connection。
- 可切模型：403/permission/insufficient balance/quota、404 model not found、连续 5xx 超限。
- 不自动恢复：用户取消、工具权限被拒、语法/参数错误。

### 模型选择

1. 读取环境变量 `HARNESS_RECOVERY_MODELS`，逗号分隔，优先级最高。
2. 若未设置，使用 `FALLBACK_MODEL_ID`。
3. 若仍未设置，从 `config/models.json` 中筛选“有 key 的不同 provider 模型”，顺序：当前 provider 外的模型优先，同 provider 后置。
4. 每个模型最多尝试一次，记录 attempted models。

### 兜底总结

当所有模型都失败时，不再空白，生成本地模板总结：

- 用户原始问题
- 主模型错误
- 子 Agent 错误/最后消息
- 已完成的工具/文件修改摘要
- 建议的恢复动作：`/model`、充值、设置 env、重试

该兜底不依赖 LLM，必须本地生成。

## 实施切分

1. `harness/ui/classic_display.py`
   - Rich/纯文本安全渲染：status footer、stats panel、agent progress panel、error fallback。
2. `harness/agent/recovery.py`
   - 增加错误分类：`is_model_recoverable_error`、`is_billing_or_permission_error`、`candidate_recovery_models`。
3. `harness/loop.py`
   - `call_llm` 在可切模型错误时尝试 recovery models。
   - 最终失败时追加本地兜底 assistant 消息并打印。
4. `harness/teams/teammate.py`
   - progress 文案改为结构化短句，工具输入摘要化。
   - error outcome 带最后 worker text、工具数、耗时。
5. `harness/cli.py`
   - 不再正常回合重复 `renderer.user(query)`，或改成极简 muted 阶段。
   - `/stats` 使用 `classic_display` 渲染，不手写 ANSI 宽度。

## 验收

- Classic `/stats` 中文、英文、ANSI 下不出现错位框线；无 Rich 时是纯文本。
- 用户输入一次任务，不再额外重复一行完整命令。
- 子 Agent 运行时至少能看到：started、running/read、done/failed。
- DeepSeek Pro 403 时自动尝试一个可用 fallback；fallback 全失败时仍返回本地兜底总结。
- 不破坏 TUI 的结构化工具卡与错误展示。
