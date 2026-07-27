# Node.js 纯终端 Agent 交互方案（已决策，待实现）

> 状态：方案已根据评审意见锁定，尚未开始编码  
> 目标：使用 Node.js 构建真实终端界面，替换渲染较卡的现有 Textual 页面；不使用网页、WebSocket、React DOM、Tauri 或 Electron。一次完成全部能力并统一验收，不先交付最小版本。

## 1. 已锁定的决策

1. 终端 UI 必须使用 **Node.js** 实现。
2. 运行位置是真实终端，不打开浏览器，也没有 URL。
3. 使用 **Ink + React（终端渲染器）**，不是 React 网页。
4. Python 继续负责 Agent 核心、模型、工具、会话、RAG、Todo 和权限策略。
5. Node.js 与 Python 通过本机子进程的 **JSON Lines（JSONL）stdin/stdout** 通信，不使用 WebSocket。
6. 界面采用**单时间线 + 底部输入区**，不做左右侧栏和大型工作台。
7. 工具成功时默认显示一行摘要；失败、阻止和权限请求显示详细内容。
8. 长任务恢复必须由用户手动确认，不能在启动后自动继续执行。
9. 不先做最小版本；按完整范围实现后统一验收。
10. 现有 Textual 暂时保留为回退入口；Node CLI 全部验收通过后，再单独决定是否删除。

## 2. 最终界面形态

```text
  improved_harness   deepseek-v4-flash   direct   ctx 31%
────────────────────────────────────────────────────────────

  You
  修复登录接口超时，并运行相关测试

  ● 请求模型  8s
  ✓ read_file   app/auth.py
  ✓ edit_file   app/auth.py
  ● bash        python -m pytest tests/test_auth.py -q  12s
  ✓ bash        12 passed
  ◇ checkpoint  round 4

  Agent
  已修复超时配置，并通过 12 项测试。

────────────────────────────────────────────────────────────
  [deepseek-v4-flash] › 输入任务…
```

主要交互：

- `Enter`：发送。
- `Shift+Enter`：换行。
- `↑ / ↓`：输入历史。
- `Ctrl+C`：运行中停止当前任务；空闲时清空输入或二次确认退出。
- `Ctrl+L`：清理当前终端显示，不删除会话。
- `/model`、`/mode`、`/resume`、`/clear`、`/skill`、`/rag`、`/usage`、`/help`：继续支持。
- 权限请求以内联卡出现，可允许、拒绝、取消；Bash 命令可编辑后允许。

## 3. 为什么不使用 WebSocket

Node CLI 与 Python Agent 在同一台机器、同一启动流程中运行。最短通信链路是父子进程管道：

```text
Node CLI
  ├── stdin  → Python：command JSONL
  └── stdout ← Python：event JSONL
```

WebSocket 会额外引入端口、服务生命周期、跨域/鉴权、断线重连和端口冲突；这些是网页或远程客户端才需要的能力。纯本地终端不需要。

## 4. 总体架构

```text
┌──────────────── Node.js 终端进程 ────────────────┐
│ Ink UI                                           │
│ ├─ Finalized Scrollback：已完成内容，不重复渲染  │
│ ├─ Live Region：只渲染当前状态和当前工具          │
│ ├─ Composer：输入、历史、命令补全                 │
│ └─ Protocol Client：JSONL 收发与子进程监管        │
└──────────────────────┬───────────────────────────┘
                       │ stdin/stdout JSONL
┌──────────────────────▼───────────────────────────┐
│ Python Agent Bridge                              │
│ ├─ 现有 agent_loop                              │
│ ├─ Provider / tools / hooks / permissions        │
│ ├─ sessions / todos / RAG / checkpoint           │
│ └─ AgentEvent Adapter                            │
└──────────────────────────────────────────────────┘
```

### 4.1 进程边界

Node 是交互主进程，负责：

- 启动和监管 Python 子进程。
- 接收键盘输入。
- 渲染终端。
- 显示连接、等待、失败和恢复状态。
- Python 意外退出后给出重启或退出选项。

Python 子进程负责：

- 加载当前工作区和会话。
- 执行 Agent loop。
- 调用模型与工具。
- 保存检查点。
- 将所有显示信息转成结构化事件。
- stdout 只输出协议 JSONL；普通诊断日志写 stderr 或日志文件。

## 5. 协议设计

每行必须是一个完整 JSON 对象，并包含协议版本和事件编号：

```json
{"v":1,"id":42,"type":"tool.started","turnId":"t-7","toolId":"call-3","name":"read_file","summary":"app/auth.py"}
```

### 5.1 Node → Python 命令

```text
client.hello
turn.start
turn.stop
permission.resolve
session.list
session.switch
session.new
command.run
shutdown
```

### 5.2 Python → Node 事件

```text
server.ready
history.snapshot
turn.started
llm.started
llm.heartbeat
llm.retry
llm.finished
tool.started
tool.finished
permission.requested
todo.updated
files.changed
checkpoint.saved
message.final
turn.stopped
turn.failed
turn.completed
server.error
```

### 5.3 协议约束

- stdout 不允许出现非 JSON 文本。
- stderr 不参与协议解析。
- 每个 turn 有唯一 `turnId`。
- 每个工具调用有稳定 `toolId`，用于原位更新。
- Node 对未知事件忽略并记录警告，避免版本升级直接崩溃。
- 单条事件设置大小上限；超大工具结果落盘，只传摘要和文件路径。
- Python 退出时，Node 必须结束 Busy 状态并显示可操作错误。

## 6. 解决终端渲染卡顿

现有终端页面容易卡，不只是语言问题，核心是长历史下的重复布局和刷新。Node UI 必须遵守以下性能约束。

### 6.1 已完成内容不重复渲染

- 已完成的用户消息、工具摘要和回答写入终端 scrollback。
- Ink 只维护屏幕底部的 Live Region。
- 新事件到来时不重新构建完整聊天历史 Widget 树。
- 会话切换时分段加载历史，不一次挂载全部内容。

### 6.2 有界内存

- UI 内只保存最近一段结构化事件。
- 完整历史以 `.project/sessions/.../session.jsonl` 为准。
- 工具完整输出继续落入 `.task_outputs/`，UI 仅显示摘要。
- 连续相同状态原位计数，不持续追加重复行。

### 6.3 限制刷新频率

- 心跳最多每秒刷新一次。
- 流式文本按时间或字符批量合并，不逐 token 触发 React render。
- 高频工具事件经过队列合并后刷新。
- 终端 resize 使用 debounce。

### 6.4 降级模式

- 非 TTY、CI、重定向输出时自动使用静态行模式。
- 低性能终端可设置 `HARNESS_NODE_STATIC=1`，关闭原位更新。
- 不依赖鼠标完成核心操作。

## 7. 解决长任务运行到一半卡死

换成 Node UI 本身不能解决后端阻塞，因此这部分列为同一次实现的强制范围。

### 7.1 模型请求超时

Provider 显式设置：

```text
connect timeout：10 秒
read timeout：90 秒（环境变量可调）
write timeout：30 秒
pool timeout：10 秒
```

不再使用 SDK 默认的 600 秒读取等待。

### 7.2 有限重试

只重试瞬时错误：

- API timeout / connection error。
- HTTP 429。
- HTTP 502 / 503 / 529。

默认最多 3 次，退避等待；认证错误、参数错误和普通 4xx 不重试。每次重试必须发出 `llm.retry` 事件。

### 7.3 心跳和无进展监控

- 模型请求期间每秒生成本地心跳，不依赖服务端返回数据。
- Node 显示等待秒数。
- 超过警戒时间显示“仍在等待模型”，而不是永久 Running。
- Python bridge 和 Node 分别维护最后事件时间，避免某一侧静默死亡。

### 7.4 停止行为

- `Ctrl+C` 发送 `turn.stop`。
- Agent 在模型轮次和工具边界检查取消状态。
- 同步 HTTP 调用依靠短 read timeout 最终退出；不得再等待 600 秒。
- 停止后修复未配对的 tool_use/tool_result。
- 释放 Agent lock，并把原问题放回输入区。

### 7.5 安全检查点

在以下边界保存：

1. 模型完整返回后。
2. 一组工具全部产生完整 tool_result 后。
3. Todo 更新后。
4. 最终回答后。

检查点记录目标、轮次、最后完成步骤和运行状态。不得在破坏性工具只执行一半时标记可安全重放。

### 7.6 重启恢复

启动发现未完成任务时显示：

```text
检测到未完成任务
目标：修复登录接口超时
最后完成：edit_file app/auth.py
停止位置：等待模型第 5 轮

[R] 从安全点继续  [E] 编辑原问题  [D] 放弃
```

默认不继续，必须由用户选择。

## 8. 文件规划

建议新增：

```text
node_cli/
├─ package.json
├─ package-lock.json
├─ tsconfig.json
├─ src/
│  ├─ cli.tsx
│  ├─ app.tsx
│  ├─ protocol.ts
│  ├─ process-manager.ts
│  ├─ state.ts
│  ├─ input.tsx
│  ├─ live-region.tsx
│  ├─ permission.tsx
│  └─ static-renderer.ts
└─ tests/

harness/node_bridge/
├─ __init__.py
├─ server.py
├─ protocol.py
├─ event_adapter.py
├─ turn_runner.py
└─ checkpoint.py
```

预计修改：

```text
main.py
requirements.txt
harness/loop.py
harness/agent/recovery.py
harness/agent/cancel.py
harness/providers/anthropic.py
harness/providers/openai_compat.py
harness/project/session*.py
harness/ui/renderer.py
```

启动入口最终提供：

```text
npm run cli                 开发运行
node_cli/bin/harness        Node CLI 入口
python main.py --node-cli   兼容启动入口
python main.py --tui        原 Textual 回退入口
python main.py --classic    原行模式回退入口
```

## 9. 一次完整实现的内部顺序

虽然不交付最小版本，但开发仍按安全顺序推进，每一步测试并保存 Git checkpoint，最终一次性交付审查：

1. 清点工作区与网页残留，建立测试基线。
2. 定义 JSONL 协议和跨语言契约测试。
3. 实现 Python bridge，不接 UI先验证事件序列。
4. 实现 Provider 超时、有限重试和心跳。
5. 实现取消、锁释放和异常收口。
6. 实现安全检查点和手动恢复。
7. 实现 Node 子进程管理与协议客户端。
8. 实现单时间线、Live Region、输入和命令交互。
9. 实现权限、会话、模式、模型、RAG和用量命令。
10. 实现有界渲染、事件合并、静态降级。
11. 接入启动入口和退出清理。
12. 运行完整故障、性能和回归测试。
13. 更新使用文档，统一交付审查。

如果开发中途退出，应从最近 Git checkpoint 继续；不得把未测试的多步修改堆在一个工作区中。

## 10. 统一验收标准

### 功能

- 普通问答、连续工具链和最终回答正常。
- 模型/模式切换、会话新建/切换、Skills、RAG、Todo、用量命令可用。
- 破坏性工具权限确认可用。
- Ctrl+C 停止、正常退出和 Python 异常退出均能恢复终端。

### 长任务可靠性

- 模拟 API 永不返回时，90 秒内超时而不是等待 600 秒。
- API 前两次失败、第三次成功时任务继续。
- Stop 后最终释放 Busy 和锁。
- 工具完成后断网，重启可从安全检查点恢复。
- 不重复执行已经完成的破坏性工具。

### 性能

- 加载长会话时不一次渲染全部历史。
- 高频心跳和流式事件不会逐条触发全页面刷新。
- 连续运行长任务时内存保持有界。
- 在 Windows Terminal 中输入、滚动和停止没有明显卡顿。
- 非 TTY 环境能退化为静态输出。

### 协议

- Python stdout 全部是合法 JSONL。
- 畸形消息、未知事件、子进程崩溃均有测试。
- 大工具结果不会直接塞入协议导致阻塞。

### 回归

- Python 现有 Agent、工具、会话、RAG和 compact 测试通过。
- `--tui` 与 `--classic` 在迁移期仍可启动。
- Node 单元测试、协议集成测试和端到端测试通过。

## 11. 不在本次范围

- 网页 UI。
- WebSocket / HTTP 服务。
- Tauri / Electron。
- 远程多用户访问。
- 图片、图表和文件拖放。
- 多栏 IDE 工作台。
- 自动恢复并执行未完成任务。

## 12. 最终建议

采用：

```text
Node.js + TypeScript + Ink
Python Agent 子进程
stdin/stdout JSONL
单时间线 + 底部输入
scrollback 与 Live Region 分离
有界事件和批量刷新
90 秒模型读取超时 + 有限重试
安全检查点 + 手动恢复
一次完整实现后统一验收
```

该方案保留 Node.js 终端开发体验，同时正面处理现有页面渲染卡顿和长任务后端阻塞；不会把问题转移到网页或 WebSocket 层。
