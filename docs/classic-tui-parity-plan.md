# Classic CLI 与 Textual TUI 差异及增强需求

## 目标

保留 Classic CLI 的滚动式、轻量、鼠标不被接管的交互方式，补齐用户当前最需要的三项能力：

1. `/resume` 使用方向键选择会话并切换；
2. `/stats` 提供与 TUI 同源的用量统计；
3. 页面最底部持续显示运行状态，而不是每轮把状态打印进聊天历史。

不把 Classic 重做成另一套 TUI，不复制工具卡、权限面板或聊天历史控件。

## 当前差异

### 共享能力

- Agent loop、工具注册、MCP、会话持久化、Todo、RAG、模型与模式配置共用同一运行时。
- `/model`、`/mode`、`/resume <N>`、`/skill`、`/clear`、`/usage`、`/rag` 等命令两边都有底层实现。
- Esc/Ctrl+C 中断最终都调用同一取消与回滚逻辑。

### TUI 特有能力

- 固定 Chat、meta、composer、后台任务和权限区域；页面不会因为每轮输出而改变布局。
- `/resume`、`/model`、`/mode`、`/skill` 使用同页 picker。
- `RuntimeMetrics` 展示 context 使用量、cache hit/miss、system/user/assistant/tool token 分解。
- 工具调用为可更新、可折叠的结构化卡片；后台任务有独立托盘。
- 同页权限确认、历史重灌、多行输入、输入历史和系统剪贴板支持。
- `/stats` 使用 Textual Screen，包含 Today/7 Days/30 Days、趋势、分模型统计和余额状态。

### Classic 当前特征与缺口

- 滚动输出，鼠标不被 Textual 捕获，适合普通终端使用。
- `/model`、`/mode` 已复用 `terminal_menu.select_from_list` 实现方向键菜单。
- `/resume` 原本只打印编号列表；当前工作区存在临时 picker 改动，但需收敛为可测试实现。
- `/stats` 当前工作区存在临时字符串拼接版本，与 TUI 重复逻辑且 ANSI 宽度可能错位。
- 状态信息若每轮 `print`，会污染滚动历史；Classic 没有固定底栏。
- `loop._publish_context_metrics` 只在 TUI active 时发布，Classic 无法获得最新 context 指标。

## 需求范围

### P0：交互式 `/resume`

- 裸 `/resume` 在交互式 TTY 中打开方向键菜单。
- 菜单行显示：序号、标题、更新时间、当前会话标记。
- 默认光标定位当前会话；Enter 切换；Esc 取消并保持当前会话。
- 非交互环境退化为现有文本列表，不能阻塞测试或管道。
- `/resume <N>`、`/resume project`、`/resume delete <N>` 保持原语义。
- 会话切换后正确更新 `binding`、Todo binding、history、context。

### P0：共享 `/stats`

- 把统计数据计算和展示模型从 `harness/ui/tui/stats.py` 抽到共享模块，例如 `harness/usage/dashboard.py`。
- TUI Screen 和 Classic `/stats` 使用同一份 Today/7 Days/30 Days、趋势、分模型数据。
- Classic 使用 Rich Table/Panel 或无 ANSI 的结构化文本，不手算包含转义符的字符串宽度。
- `/stats` 不做同步余额网络请求；余额查询继续由显式 `/balance` 承担。
- 用量为空时正常显示“暂无记录”。

### P0：Classic 固定底栏

- 底栏显示：模型、模式、context 使用百分比、最近一次 cache hit rate、已连接 MCP 数量或健康状态。
- 底栏不进入聊天历史，不在每轮追加一行。
- 使用 Rich `Live` 或兼容 readline 的单行重绘；输入提示始终位于底栏上方或与底栏协调，不遮挡用户正在输入的内容。
- 工具/后台消息输出后恢复输入行与底栏。
- 非 TTY、Rich 不可用、Windows GBK 或终端能力不足时自动退化为普通提示符，不输出控制字符垃圾。
- Classic 退出时恢复光标和终端状态。

### P0：共享 RuntimeMetrics 快照

- 去掉 `loop._publish_context_metrics` 对 `is_tui_active()` 的硬依赖。
- 引入线程安全的运行时指标 store/snapshot；loop 更新 context 指标，LLM usage 更新 cache 指标。
- TUI bridge 与 Classic 底栏都订阅或读取同一个快照。
- 不让 UI 代码反向依赖 agent loop。

## 非目标

- 不在 Classic 中实现固定 Chat 面板或全屏布局。
- 不复制 TUI 工具折叠卡、后台任务托盘、同页权限卡。
- 不改变默认是否启动 TUI 的策略。
- 不修改 Agent 行为、Prompt、压缩、缓存策略或会话存储格式。
- 不覆盖工作区中与 session binding、TUI 相关的既有改动。

## 验收标准

1. 交互式 TTY 输入 `/resume` 可用 Up/Down/Enter/Esc 完成选择；非 TTY 返回列表。
2. `/stats` 在 Classic 与 TUI 对同一 mocked usage 数据产生一致的核心数值。
3. Classic 连续完成两轮对话后，状态只保留在屏幕底部，不在输出历史中重复两行。
4. 状态至少包含 model、mode、context%、cache hit%、MCP 状态。
5. 输入过程中异步工具消息或 cron 消息不会吃掉输入文本。
6. Windows 与非 Windows 的菜单按键测试通过；无 Rich 环境可退化。
7. 原有 `tests/test_cli_commands.py`、`tests/test_tui_m1.py`、`tests/test_resume.py`、`tests/test_usage.py` 全部通过。
8. 新增针对 resume picker、共享 dashboard、runtime metrics store、classic footer fallback 的单元测试。

## 建议文件边界

- `harness/ui/classic/status_line.py`：Classic 底栏生命周期和终端重绘。
- `harness/ui/resume_picker.py`：会话标签生成和方向键选择，避免逻辑堆进 `cli.py`。
- `harness/runtime_metrics.py`：线程安全快照和更新 API。
- `harness/usage/dashboard.py`：共享统计 view model。
- `harness/ui/tui/stats.py`：只负责 Textual 渲染。
- `harness/cli.py`：只做命令路由与生命周期连接。

## 实施顺序

1. 先抽 runtime metrics store 和 usage dashboard，保持现有 TUI 行为。
2. 实现 `/resume` picker 并补测试。
3. 实现 Classic `/stats`，复用共享 dashboard。
4. 实现 Classic status line 与 renderer/readline 协作。
5. 跑定向测试和全量测试，最后人工在 Windows Terminal 验证方向键、异步输出和终端复位。
