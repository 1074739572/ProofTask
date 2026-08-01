# 008 — Textual TUI（M2 同页交互）

## 状态更新（2026-07 清理）

**Textual TUI 已移除**：`harness/ui/tui/` 与 `textual` 依赖不再存在，`main.py` 默认即 classic Rich CLI。本记录的设计取舍（分区展示、工具卡原位更新、欢迎页 hero、同页权限面板、Ctrl+C 复制等）仍作为当前 classic UI 的行为参考；权限面板与工具步骤展示实现在 `harness/ui/`（`permission_prompt.py` / `tool_display.py` / `renderer.py`）。

## 痛点

经典 Rich 行模式难以做成「Kimi / 千问」式分区；终答与步骤拆开也不好回看历史。

## 锁定方案（grill-me）

| 项 | 选择 |
|----|------|
| 图标 | Emoji |
| 主区 | **单框聊天历史**（合并原 Steps+Answer） |
| 工具行 | 结构化可折叠卡片；running / ok / failed / blocked 原位更新 |
| 灌历史 | S1 启动加载当前 session |
| 顶栏 | 今日/周用量、缓存命中率 |
| 聊天下方 | 🤖 模型 · 🧭 模式 · context/cache 实时指标 · 状态 |
| 终答 | R3 Markdown；User/工具纯文本 |
| 权限 | **同页内联面板**；允许 / 拒绝 / 取消；bash 命令可编辑后允许 |

## M2：不再跳屏

权限确认不再 `push_screen(AllowModal)`。工作线程通过 `TuiBridge.ask_permission()`
等待，主线程在当前 footer 内展示 `#interaction-panel`，用户选择后用
`PermissionResponse` 唤醒工作线程。这样仍保留“工具执行前必须阻塞”的安全语义，
但不会失去当前对话、工具上下文和输入框。

- destructive bash 会展示并允许编辑命令；编辑后的值回写本次 `tool_input`
- MCP destructive tool 使用同一面板，但工具名只读
- Esc 在权限面板打开时仅取消这次授权，不会先中断整轮任务
- 旧 `AllowModal` 只为导入兼容保留，不在正常路径使用

结构化事件集中在 `harness/ui/tui/events.py`。Renderer 不再把 TUI 工具调用先压成
不可恢复的字符串，而是发送带 `tool_use_id` 的 `ToolEvent`。工具卡从 running
原位更新为 ok / failed / blocked；连续重复调用合并到上一张卡并显示 `×N`。
重新加载 session 时，`chat_history.iter_history_items()` 会配对 tool_use/tool_result，
所以历史与实时展示使用同一种卡片。

## 固定信息区

- **回答只在 Chat 时间线里**：用户消息、工具卡、助手回答、API 错误同一滚动区，可回看历史
- 已去掉独立 `#answer-dock`（原先 max-height 过短，读长文不舒服，也和 Chat 割裂）
- `#progress-strip` 显示理解目标 → 当前工具 → 回答的简短阶段
- 后台命令通过 `BackgroundEvent` 进入 `#background-tray`，展示运行与完成状态；
  启动消息不再用绕过 TUI 的裸 `print`
- meta 行显示当前 context 占比、最近一次 API cache hit rate，以及 tool/net 健康灯；
  顶栏显示今日/周累计
- 相同连续 step 日志在 widget 层原位计数，避免时间线被重复文本刷屏

## 输入区增强

- 输入 `/` 时内联显示命令提示
- Ctrl+↑ / Ctrl+↓ 浏览本次 TUI 的输入历史
- `/usage [today|week|month|year]` 可直接在 TUI 查看 token 与缓存统计
- 原有 Enter 发送、Shift+Enter 换行、Ctrl+Enter 发送、Esc 停止保持不变
- **粘贴**：Ctrl+V / Ctrl+Shift+V 读取**系统剪贴板**（Win32，64 位句柄安全；失败再试 PowerShell / tk）。即使焦点在 Chat 区也会路由到输入框。注意：Textual 开启鼠标模式后，**右键不会粘贴**（会被当成鼠标事件）——请用 Ctrl+V。
- **API 错误**：失败以红色 `bubble-error` 出现在 Chat 时间线（不再伪装成绿色终答栏）

## 打断 / 退出（X1 · B2 · U1）

| 项 | 行为 |
|----|------|
| 退出 TUI | `request_cancel` + 吞掉 console 输出；等当前轮结束后再退 |
| 主按钮 | 空闲 **发送** / 忙碌 **停止**（Enter / Ctrl+Enter 同；Esc = 停止） |
| Ctrl+C | **复制**到系统剪贴板（有选区复制选区；否则复制最近回答；**不退出**） |
| Ctrl+Shift+C / Ctrl+Shift+Y | 强制复制 Chat 里最近一条助手回答（Y 备用：WT 可能抢走 Ctrl+Shift+C） |
| Ctrl+V | 从系统剪贴板粘贴到输入框 |
| 退出 | Ctrl+Q |
| 输入区 | **TextArea** 多行：Enter 发送；Shift+Enter 换行；Ctrl+↑/↓ 历史；高度约 3–8 行 |
| 停止效果 | 回滚本轮 history + 撕掉 `turn-live` 气泡 + 输入框预填原问题 |
| 终端复位 | 启动前 / 退出后 / `atexit` 清鼠标上报与 alt-screen（防 `^[[<…M` 花屏） |

若仍花屏：关掉该终端标签，**新开**一个再 `python main.py`（同一脏标签里重启不够时）。

```sh
python main.py              # 默认 TUI
python main.py --classic    # Rich 行模式
HARNESS_TUI=0 python main.py
```

## 欢迎页（Chat 置顶）

| 项 | 选择 |
|----|------|
| 位置 | 每次进入 Chat 顶部，下方接历史（W1） |
| Hero | 瘦身品牌行：小空心笑脸 + `HELLO` + 淡 tagline（宽屏并排 / 窄屏一行） |
| 配色 | 笑脸 `#E6B84D` · HELLO `#7FDBFF` · 引用卡琥珀竖线 |
| 每日一句 | **引用卡**：`TODAY` + 引号正文 + 右对齐来源；Hitokoto 队列 F1 |
| 动效 | 进场一次：品牌 → 引用卡 → 渐变细线 |
| 分隔 | 琥珀→亮青渐变 `━` 细线 |
| 会话表 | 已去掉 |

```sh
python -m harness.ui.tui.quotes status
python -m harness.ui.tui.quotes refill   # 手动灌满本地队列
python -m harness.ui.tui.quotes today
```

队列文件：`.project/daily_quotes.json`

## 会话斜杠（与 classic 对齐）

| 命令 | TUI 行为 |
|------|----------|
| `/resume` | Chat 列出会话 + 内联 picker 切换 |
| `/resume <N>` | 切换会话，`reload_session_view` 重灌 Chat |
| `/resume project` | 注入 `[Resume context]` |
| `/resume delete <N>` | 删会话目录（删当前则开新会话） |
| `/clear` | 清对话 + 默认清 `state.json` |
| `/clear session` | 只清对话，保留长任务 |
| `/skill` | 列 skill + picker；`/skill <name>` 注入全文（Chat 仅短提示） |
| `/usage` | 在 Chat 展示 token、cache hit rate 与模型分项 |

忙碌中禁止切换；需先 Stop。

## 关键文件（设计时引用；Textual 已移除，下表仅存档）

| 路径（已移除） | 作用（能力去向） |
|------|------|
| `harness/ui/tui/app.py` | 布局 · 同页权限 · 工具/后台状态 · 固定终答 · composer（→ classic：`harness/ui/renderer.py`） |
| `harness/ui/tui/events.py` | 事件模型（→ `harness/ui/events.py` / `harness/event_stream.py`） |
| `harness/ui/tui/bridge.py` | worker → 主线程桥接（→ `harness/ui/permission_events.py`） |
| `harness/ui/tui/commands.py` | 斜杠命令（→ `harness/cli.py`） |
| `harness/ui/tui/chat_history.py` | session → 结构化聊天（→ `harness/project/transcript.py`） |
| `harness/ui/tui/usage_bar.py` | 用量与 cache hit rate（→ `harness/usage/report.py`） |
| `harness/ui/tui/widgets.py` | MetaChip / ToolCard（→ `harness/ui/tool_display.py`） |
| `harness/ui/renderer.py` | 结构化工具事件入口 |
| `harness/agent/background.py` | 后台任务状态事件；精确慢命令识别 |
