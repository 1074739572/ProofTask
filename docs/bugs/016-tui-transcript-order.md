# 016 — TUI 转写顺序与呈现（response 在工具之后 · 展开按步 · plan 沉底 · 全白）

**状态：** 已修复（颜色需在真实终端用探针复核）  
**影响：** `node_tui`（src-open OpenTUI 前端）日常使用  
**关联：** [006 终答看不见](./006-final-answer-buried.md)（Rich CLI 侧）· [008 Textual 存档](./008-textual-tui-m1.md)  
**证据：** 2026-08-08 用户：「现在的设计页面一塌糊涂——response 不能放工具调用后面吗、展开都堆在一起看不了每一步调了什么、plan 一进会话就放最上面、颜色一直是白色」。

---

## 现象

1. **response 位置错**：整轮所有文本（含最终答案）合并成一个 response 段，排在工具调用之前；折叠后的「Turn complete」又被追加在最末尾 → 用户先读到答案、再翻到工具过程，顺序是反的。
2. **展开一团**：`[ 展开过程 ]` 后按类型分组罗列——所有 💭 意图一段、所有工具一段（同名调用被压成 `bash · Called 2 times`）、子 agent 一段，看不到「第几步调了什么工具」的时间顺序。
3. **plan 顶置**：`task_update` 在会话一开始就入转写流（resume 时旧 todos 直接发射），面板钉在最上面。
4. **颜色全白**：无 `WT_SESSION` / `TERM` / `COLORTERM` / `TERM_PROGRAM` 的 Windows 控制台窗口（explorer 双击启动、conhost）里，一切渲染为默认前景色。

---

## 根因

| # | 根因 | 位置 |
|---|------|------|
| 1 | `responseId` 整轮不重置：`assistant_delta` 全部追加进首个 response entry；`agent_end` 折叠又把 summary 追加到末尾 | `src-open/App.tsx` 事件处理 |
| 2 | `buildSections` 折叠时按类型各存一份（intents / rows / subagents），展开视图分组渲染；同名合并只留 count | `src-open/sections.ts` · `App.tsx` |
| 3 | tasks entry id 固定（`tasks:current`），首次出现的位置即永久位置 | `sections.ts` |
| 4 | opentui 原生渲染器用「环境变量白名单 + 终端查询」判定 rgb/ansi256；裸 conhost 两者都拿不到 → 能力判定为无 → 颜色被抹成默认 | `@opentui/core` 原生层；应用侧无任何补救 |

---

## 已改

### 1 · response 分段 + summary 锚在「工作结束处」

- `tool_start` / `subagent_start` 时先 `flushDeltasNow()` 再 `responseId = ''`：文本在工具后恢复时开新段，各段按时间序交错。
- summary 折叠后插入位置从「末尾」改为 `max(被折索引) + 1 - 被折个数`：折叠块落在**工作结束处**——轮中过渡文本在折叠之上、最终答案在折叠之下。

```
│ › 修复登录接口的 500 错误        ← prompt
│ 好的，我先看一下…               ← 过渡文本（折叠之上）
▾ Turn complete · 3 工具 · (4s)   ← 折叠块（工具/思考/子agent 收在里面）
│ 问题定位到了：routes.py…        ← 最终答案（折叠之下）
```

### 2 · 展开视图 = 编号步骤列表（时间序）

- summary 折叠时构建统一 `steps: SummaryStep[]`（intent / tool / subagent 交错，扫描即时间序）。
- 同名合并行保留 `calls[]` 明细；折叠展开时按每次调用逐条展开（不再 `Called N times` 抹平）。
- 渲染：`1. 💭 意图` / `2. ✓ bash  命令 (0.9s)` / `3. ✕ npm_install` + `└ 错误` / `4. ✓ subagent code · 2 tools — 摘要`，末尾仍是「文件变更」。

### 3 · plan 沉底 + 空计划不渲染

- tasks 不再按首现位置渲染：只保留最新快照，作为**最后一个 section**（贴近 composer，实时状态位，对齐 opencode 的「live todo 独立于消息流」）。
- 空任务数组不画空框。

### 4 · 颜色（conhost 全白）

- 新增 `src-open/env.ts`（`index.tsx` 首行 import）：win32 且没有任何终端能力变量时，补 `COLORTERM=truecolor` + `TERM=xterm-256color`（Win10 1607+ 的 conhost / PyCharm / VS Code 均支持 24-bit VT 色）。`HARNESS_TUI_NO_ENV_HINTS=1` 可关。
- 新增探针 `npm run probe:color`（`src-open/probe_color.tsx`）：用与真实 TUI 相同的渲染路径画调色板，并把原生探测结果写入 `node_tui/color_probe.json`（rgb / ansi256 / terminal）。

非 TTY 冒烟：无论 hint 开否，探测都输出 `38;2;R;G;B` 真彩序列且 `rgb=true`——真实 TTY 的 conhost 路径仍待用户实机复核。

### 5 · 顺带

- debug 帧可用了：`App` 在 `debugEntries` 下跳过欢迎页（此前只能看 welcome）。
- response/prompt 左沟槽 `│ ` 改用结构间距（`gap`/`paddingLeft`），不再依赖尾部空格。

---

## 相关文件

```text
node_tui/src-open/sections.ts          # steps 折叠 · calls 明细 · plan 沉底
node_tui/src-open/App.tsx              # response 分段 · 展开步骤列表 · sig · 沟槽
node_tui/src-open/env.ts               # win32 颜色能力提示（新）
node_tui/src-open/probe_color.tsx      # 颜色/能力探针（新）
node_tui/src-open/index.tsx            # 首行 import env.ts
node_tui/src-open/debug.tsx            # 脚手架：跳欢迎页 + 新转写样例
node_tui/package.json                  # probe:color 脚本
node_tui/test/src_open_sections.test.ts # 新：顺序/展开/沉底/空计划
node_tui/test/src_open_subagent.test.ts # 适配 steps 结构
```

---

## 仍观察

| 项 | 说明 |
|----|------|
| conhost 实机颜色 | 需在真实终端跑 `npm run probe:color`：若仍白且 `rgb=false ansi256=false`，再加 16/256 色回退主题 |
| 折叠内输出 | 展开步骤列表暂不含工具 stdout 尾部（避免又堆成一团）；有需要再加「每步 Enter 看输出」 |
| 长回合折叠位置 | 过渡文本夹在意图之间时以「工作结束处」为锚，多段文本都在折叠块之外（顺序正确，块可更瘦） |
