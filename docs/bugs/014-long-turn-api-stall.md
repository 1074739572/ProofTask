# 014 — 长任务等待模型时 TUI 假死

## 状态

**已定位，延期修复。** 当前阶段优先改 Agent 交互页面；本记录用于后续恢复问题上下文，不在页面改版中顺手修改网络与执行链。

## 现象

长任务执行到一半后，页面长期停留在 Running，不再出现推断、工具调用或回答；退出再进入时，用户难以判断任务做到了哪里。

## 本地证据

会话记录中已出现两类明确错误：

- `APITimeoutError: Request timed out`
- `APIConnectionError: Connection error`

长会话 `1784966103_f1f233ec` 有 422 条消息、211 次 assistant 轮次；用量记录显示上下文曾升至约 39k token。会话中没有未配对的 `tool_use`，因此当前证据不支持“工具消息损坏导致停止”。

## 已定位根因

1. `agent_loop` 在 TUI worker 中同步等待模型 SDK。
2. Anthropic/OpenAI SDK 当前默认 read timeout 为 600 秒。
3. 等待期间没有周期性心跳，页面只能保持 Running。
4. Esc/Stop 使用协作式取消标志，无法中断正在阻塞的 HTTP 请求。
5. 阻塞期间 worker 与 `agent_lock` 未释放，因此新推断和工具执行无法开始。

这不是单纯的 Textual 渲染问题；仅把界面换成 React/TypeScript 也不会自动修复后端阻塞。

## 后续修复边界

按小步、可恢复方式处理：

1. 增加模型请求耗时与心跳事件。
2. 显式配置 connect/read/write/pool timeout。
3. 对 timeout、connection、429、502/503/529 做有限重试。
4. 让 Stop 能终止或快速结束当前请求，而非只设置标志。
5. 在完整工具轮次后保存安全 checkpoint。
6. 增加未完成任务恢复入口与 watchdog。

每一步独立测试、独立提交；页面改版阶段不得混入以上行为修改。

## 相关文件

- `harness/loop.py`
- `harness/agent/recovery.py`
- `harness/agent/cancel.py`
- `harness/providers/anthropic.py`
- `harness/providers/openai_compat.py`
- `harness/providers/router.py`
- `harness/cli.py`（等待时的 UI 状态）
- `harness/ui/renderer.py`
