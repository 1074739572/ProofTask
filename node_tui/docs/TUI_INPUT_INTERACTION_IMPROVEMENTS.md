# Node TUI 输入与交互改进计划

> 文档版本：2026-08-18
> 基于当前 `node_tui/src-open/App.tsx` + `src/autocomplete.ts` 的代码审查，对照 dsh-TUI 终端交互设计

---

## 一、现状总结

### 当前已具备的能力

| 能力 | 状态 | 位置 |
|------|------|------|
| Enter 发送 | ✅ | App.tsx `submit()` |
| Shift+Enter 换行 | ✅ | App.tsx `textareaBindings` |
| 多行输入（最多 5 行） | ✅ | App.tsx `MAX_COMPOSER_LINES` |
| 上下键历史记录 | ✅ | App.tsx `inputHistory` |
| `/` 命令补全 | ✅ | autocomplete.ts |
| `@` 文件补全 | ✅ | autocomplete.ts |
| Tab/Enter 确认补全 | ✅ | App.tsx `selectCompletion()` |
| Esc 关闭补全 | ✅ | App.tsx `closeCompletion()` |
| 运行中禁止重复发送 | ✅ | App.tsx `submit()` |
| Ctrl+K 中断 | ✅ | App.tsx keyboard handler |
| 输入框自动增高 | ✅ | App.tsx `composerLines()` |
| 输入状态与后端同步 | ✅ | App.tsx event handler |
| 鼠标滚轮滚动 | ✅ | App.tsx `onMouseScroll` |
| Toast 反馈 | ✅ | App.tsx `showToast()` |

### 与 dsh-TUI 的核心差距

| 维度 | 当前实现 | dsh-TUI |
|------|----------|---------|
| 运行中输入 | 禁止发送 | 可排队等待 |
| 历史记录 | 内存、简单追加 | 持久化、去重、搜索 |
| 补全 | 基础列表 | 分组、描述、路径类型、目录浏览 |
| 粘贴处理 | 无特殊处理 | 大文本折叠、行数/大小提示 |
| 快捷键 | 最小集 | Shell 风格完整集 |
| 状态反馈 | phase + spinner | 工具名、队列数、等待状态 |
| 错误恢复 | 日志 | 输入框可交互、重连提示 |

---

## 二、改进项清单

### 优先级说明

- **P0**：直接影响用户体验，应最先实现
- **P1**：提升专业感和效率
- **P2**：向 dsh-TUI 看齐的高级特性

---

### P0-01：运行中消息队列

**当前问题**

```text
用户输入消息 → Agent 正在运行 → 提示"Agent is already running"
```

用户必须先 Ctrl+K 中断，才能发送新消息。

**目标行为**

```text
用户输入消息 → Agent 正在运行 → Enter 显示为"Queue" → 消息加入队列
→ Agent 完成当前任务 → 自动发送队列中的消息
```

**实现建议**

```typescript
// App.tsx 新增信号
const [messageQueue, setMessageQueue] = createSignal<string[]>([]);

// submit() 修改
const submit = () => {
  const text = input().trim();
  if (!text) return;
  
  if (running() && !isGoalControl) {
    // 排队而非拒绝
    setMessageQueue(prev => [...prev, text]);
    setInput('');
    textareaRef?.setText?.('');
    showToast(`消息已排队 (${messageQueue().length + 1)} 条`);
    return;
  }
  // ... 原有发送逻辑
};

// Agent 完成后自动发送队列
createEffect(() => {
  if (!running() && messageQueue().length > 0) {
    const next = messageQueue()[0];
    setMessageQueue(prev => prev.slice(1));
    setInput(next);
    textareaRef?.setText?.(next);
    // 延迟一帧再提交，确保 UI 更新
    setTimeout(() => submit(), 0);
  }
});
```

**底部提示变化**

```text
空闲：Enter send · Shift+Enter newline
运行中：Enter queue · Ctrl+K interrupt
队列中有消息：Enter queue (2 pending) · Ctrl+K interrupt
```

---

### P0-02：动态输入提示

**当前问题**

底部提示固定显示 `phase · spinner · elapsed · Ctrl+K 中断`，不随输入状态变化。

**目标行为**

底部提示根据当前状态动态变化：

| 状态 | 提示内容 |
|------|----------|
| 空闲、无输入 | `Enter send · Shift+Enter newline` |
| 空闲、有输入 | `Enter send · Shift+Enter newline · 3 lines` |
| 运行中 | `● working · bash · 12s · 3 tools · Enter queue · Ctrl+K interrupt` |
| 运行中、等待权限 | `⚠ waiting for approval · Enter approve · Esc deny` |
| 补全激活 | `↑↓ select · Tab apply · Esc close` |
| 后端断开 | `Backend unavailable · Enter retry · Ctrl+R reconnect` |

**实现建议**

```typescript
const footerHint = () => {
  if (overlay()?.kind === 'permission') return 'Enter approve · Esc deny';
  if (completion().mode) return '↑↓ select · Tab apply · Esc close';
  if (!backendReady()) return '正在连接后端…';
  if (running()) {
    const tools = turnToolCount > 0 ? ` · ${turnToolCount} tools` : '';
    return `● ${phase()} · ${spinner()} ${elapsed()}${tools} · Enter queue · Ctrl+K interrupt`;
  }
  const lines = composerLines() > 1 ? ` · ${composerLines()} lines` : '';
  const queue = messageQueue().length > 0 ? ` · ${messageQueue().length} pending` : '';
  return `Enter send · Shift+Enter newline${lines}${queue}`;
};
```

---

### P0-03：历史记录增强

**当前问题**

- 连续相同消息重复保存；
- 仅内存存储，退出丢失；
- 无搜索能力；
- 多行消息回填后光标位置不明确。

**目标行为**

- 去重：连续相同消息不重复追加；
- 持久化：保存到 `~/.project/tui_history.json`；
- 搜索：`Ctrl+R` 进入历史搜索模式；
- Draft：上下键切换历史时，保留当前编辑内容作为 draft。

**实现建议**

```typescript
// 去重
const addToHistory = (text: string) => {
  setInputHistory(prev => {
    const last = prev[prev.length - 1];
    if (last === text) return prev; // 连续相同不重复
    return [...prev.slice(-50), text];
  });
};

// 持久化
const HISTORY_FILE = path.join(repoRoot, '.project', 'tui_history.json');

const saveHistory = () => {
  try {
    fs.writeFileSync(HISTORY_FILE, JSON.stringify(inputHistory(), null, 2));
  } catch {}
};

const loadHistory = () => {
  try {
    if (fs.existsSync(HISTORY_FILE)) {
      const data = JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf-8'));
      if (Array.isArray(data)) setInputHistory(data.slice(-50));
    }
  } catch {}
};

// Draft 机制
const [draft, setDraft] = createSignal('');

const recallHistory = (idx: number) => {
  const hist = inputHistory();
  if (idx < 0 || idx >= hist.length) return;
  if (historyIdx() === -1) {
    // 从当前输入进入历史，保存 draft
    setDraft(input());
  }
  setHistoryIdx(idx);
  const val = hist[idx];
  setInput(val);
  textareaRef?.setText?.(val);
};

const exitHistory = () => {
  // 恢复 draft
  setHistoryIdx(-1);
  setInput(draft());
  textareaRef?.setText?.(draft());
  setDraft('');
};
```

**Ctrl+R 搜索模式**

```typescript
const [historySearch, setHistorySearch] = createSignal<{
  active: boolean;
  query: string;
  matches: string[];
  selected: number;
}>({ active: false, query: '', matches: [], selected: 0 });

// 搜索结果显示在输入框上方
// Enter 确认选中项
// Esc 退出搜索
```

---

### P0-04：粘贴大文本处理

**当前问题**

粘贴大段代码/日志时，输入框瞬间增高，无任何提示。

**目标行为**

```text
用户粘贴 82 行代码
→ 显示提示：Pasted 82 lines · 6.2 KB
→ 输入框显示折叠视图：[Paste: 82 lines, 6.2 KB]
→ Ctrl+O 展开/收起
→ 发送时自动展开
```

**实现建议**

```typescript
const [pastedContent, setPastedContent] = createSignal<{
  text: string;
  lines: number;
  size: number;
  expanded: boolean;
} | null>(null);

// 检测粘贴（通过 onContentChange 的变化量判断）
const onContentChange = (newText: string) => {
  const oldText = input();
  const diff = newText.length - oldText.length;
  
  // 粘贴检测：短时间内增加大量字符
  if (diff > 100 && !pastedContent()) {
    const lines = newText.split('\n').length;
    const size = new TextEncoder().encode(newText).length;
    setPastedContent({ text: newText, lines, size, expanded: false });
    // 显示折叠视图
    const folded = `[Paste: ${lines} lines, ${formatSize(size)}]`;
    setInput(folded);
    textareaRef?.setText?.(folded);
    showToast(`Pasted ${lines} lines · ${formatSize(size)}`);
    return;
  }
  
  setInput(newText);
  refreshCompletion(newText);
};

// Ctrl+O 展开/收起
if (event?.ctrl && name === 'o') {
  const paste = pastedContent();
  if (paste) {
    if (paste.expanded) {
      const folded = `[Paste: ${paste.lines} lines, ${formatSize(paste.size)}]`;
      setInput(folded);
      textareaRef?.setText?.(folded);
    } else {
      setInput(paste.text);
      textareaRef?.setText?.(paste.text);
    }
    setPastedContent({ ...paste, expanded: !paste.expanded });
  }
}

// 发送时自动展开
const submit = () => {
  const paste = pastedContent();
  let text = input().trim();
  if (paste && !paste.expanded) {
    text = paste.text.trim();
  }
  // ... 发送逻辑
  setPastedContent(null);
};
```

---

### P0-05：后端故障恢复

**当前问题**

后端退出后，日志显示 `backend exited (code)`，但输入框无明确恢复路径。

**目标行为**

```text
后端断开
→ 底部显示：Backend unavailable (exit code 1) · Enter retry · Ctrl+R reconnect
→ 用户按 Enter → 自动重启后端
→ 重连成功 → 恢复正常状态
```

**实现建议**

```typescript
const [backendState, setBackendState] = createSignal<'connected' | 'disconnected' | 'reconnecting'>('connected');

const retryBackend = () => {
  setBackendState('reconnecting');
  showToast('正在重连后端…');
  // 重启后端进程
  const newBackend = startBackend(onEvent, onDiagnostic, { cwd: cwd() });
  setBackend(newBackend);
};

// 在 footerHint 中处理
if (backendState() === 'disconnected') {
  return 'Backend unavailable · Enter retry · Ctrl+R reconnect';
}
if (backendState() === 'reconnecting') {
  return 'Reconnecting…';
}
```

---

### P1-01：Shell 风格快捷键

**目标快捷键集**

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+A` | 移动到行首 |
| `Ctrl+E` | 移动到行尾 |
| `Ctrl+U` | 清空当前行 |
| `Ctrl+W` | 删除前一个单词 |
| `Ctrl+K` | 删除到行尾 |
| `Ctrl+Y` | 粘贴上次删除内容 |
| `Ctrl+D` | 删除光标后字符 |
| `Ctrl+P` | 上一条历史（同 ↑） |
| `Ctrl+N` | 下一条历史（同 ↓） |
| `Ctrl+R` | 历史搜索 |
| `Ctrl+O` | 粘贴内容展开/收起 |
| `Ctrl+L` | 清空屏幕（已实现） |

**实现建议**

```typescript
const keyBindings = [
  { name: 'a', ctrl: true, action: 'beginning-of-line' },
  { name: 'e', ctrl: true, action: 'end-of-line' },
  { name: 'u', ctrl: true, action: 'kill-line-backward' },
  { name: 'w', ctrl: true, action: 'kill-word' },
  { name: 'k', ctrl: true, action: 'kill-line-forward' },
  { name: 'y', ctrl: true, action: 'yank' },
  { name: 'r', ctrl: true, action: 'history-search' },
  { name: 'o', ctrl: true, action: 'toggle-paste' },
  { name: 'return', action: 'submit' },
  { name: 'return', shift: true, action: 'newline' },
];
```

---

### P1-02：补全增强

**当前补全的不足**

- 最多显示 6 项，无滚动；
- 无命令描述；
- 无文件类型区分；
- 无目录继续展开；
- 光标移动不触发重新计算。

**目标改进**

```text
▶ /goal           Goal management
  /model          Switch model
  /status         Show status
  /compact        Compress context
─────────────────────────────────
命令 · ↑↓ select · Tab/Enter apply · Esc close

▶ @src-open/App.tsx       TSX  component
  @src/backend.ts         TS   module
  @node_tui/              DIR  3 files
─────────────────────────────────
文件引用 · ↑↓ select · Tab insert · Esc close
```

**实现建议**

```typescript
// 扩展补全选项类型
type CompletionOption = {
  label: string;        // 显示文本
  description?: string; // 描述
  icon?: string;        // 图标
  type?: string;        // 类型标记
  isDirectory?: boolean; // 是否可继续展开
};

// 光标移动时重新计算
useKeyboard((event) => {
  if (['left', 'right'].includes(event.name)) {
    setTimeout(() => refreshCompletion(), 0);
  }
});
```

---

### P1-03：输入框状态栏

**目标设计**

```text
┌─ Input ─────────────────────────────────────────┐
│ deepseek · deepseek-v4-flash · reasoning: max   │
│ ▸ Ask anything...                               │
│                                                 │
│ Enter send · Shift+Enter newline · 1 line       │
└─────────────────────────────────────────────────┘
```

或更紧凑的版本：

```text
deepseek · deepseek-v4-flash › Ask anything...
Enter send · Shift+Enter newline
```

**实现建议**

将当前的 `mode · model › textarea` 布局改为：

```tsx
<box flexDirection="column" border borderStyle="rounded" borderColor={C.accent}>
  {/* 状态栏 */}
  <box paddingX={1}>
    <text fg={C.primary}>{mode()}</text>
    <text fg={C.textMuted}> · </text>
    <text fg={C.primary}>{model()}</text>
    <text fg={C.textMuted}> · </text>
    <text fg={C.info} selectable={false} onMouseUp={openEffortPicker}>
      {effortShortLabel(effortLabel(), effort())} ▾
    </text>
  </box>
  {/* 输入区 */}
  <textarea ... />
  {/* 提示栏 */}
  <box paddingX={1}>
    <text fg={C.textMuted}>{footerHint()}</text>
  </box>
</box>
```

---

### P1-04：运行状态详细显示

**当前状态显示**

```text
• working · | · 12s · Ctrl+K 中断
```

**目标状态显示**

```text
● working · bash · 12s · 3 tools · Enter queue · Ctrl+K interrupt
```

或更详细的：

```text
● running tool: bash (cd /tmp && ls -la) · 12s · 3/5 tools
  ↳ reading file config.json · 2s
```

**实现建议**

```typescript
const [currentTool, setCurrentTool] = createSignal<string | null>(null);
const [toolProgress, setToolProgress] = createSignal({ done: 0, total: 0 });

// 在事件处理中更新
case 'tool_start':
  setCurrentTool(value(event, 'name'));
  setToolProgress(prev => ({ ...prev, total: prev.total + 1 }));
  break;

case 'tool_end':
  setCurrentTool(null);
  setToolProgress(prev => ({ ...prev, done: prev.done + 1 }));
  break;
```

---

### P2-01：鼠标文本选区与复制

**当前问题**

- 开启了 `useMouse: true`，鼠标事件被应用接管；
- 大量文本标记为 `selectable={false}`；
- 无法拖拽选择文本并复制。

**目标行为**

- 支持鼠标拖拽选择文本；
- 双击选择单词；
- 三击选择整行；
- 选中后自动复制到剪贴板（OSC 52 + 原生兜底）；
- `Ctrl+Shift+C` 手动复制选中内容。

**实现建议**

接入 OpenTUI 的 Selection API：

```typescript
import { Clipboard, Selection } from '@opentui/core';

// 在 App 根组件中
const clipboard = new Clipboard();
const selection = new Selection();

// 监听选区变化
selection.onChange((sel) => {
  if (sel.hasSelection) {
    const text = selection.getSelectedText();
    clipboard.copyToClipboardOSC52(text);
  }
});
```

---

### P2-02：`@` 文件浏览器

**当前行为**

输入 `@` 后显示文件名列表，只能选择。

**目标行为**

```text
@src/
  ├── components/
  │   ├── App.tsx
  │   └── Welcome.tsx
  ├── hooks/
  └── utils.ts
```

- 支持目录展开；
- 支持相对路径显示；
- 支持文件类型图标；
- 支持最近使用文件；
- 支持模糊搜索。

---

### P2-03：`/` 命令分组菜单

**当前行为**

输入 `/` 后显示扁平命令列表。

**目标行为**

```text
┌─ Commands ──────────────────────┐
│ ▸ Session                       │
│   /new    New session           │
│   /clear  Clear screen          │
│   /compact Compress context     │
│ ▸ Model                         │
│   /model  Switch model          │
│   /effort Set reasoning effort  │
│ ▸ Goal                          │
│   /goal   Goal management       │
│   /plan   Plan mode             │
└─────────────────────────────────┘
```

---

### P2-04：粘贴内容折叠与展开

**详细设计**

```text
┌─ Pasted content ────────────────┐
│ 82 lines · 6.2 KB · code       │
│ [Click to expand / Ctrl+O]     │
└─────────────────────────────────┘
```

展开后：

```text
┌─ Pasted content (82 lines) ─────┐
│ 1 │ function hello() {          │
│ 2 │   console.log('world');     │
│ 3 │ }                           │
│ ...                             │
│ 82│ }                           │
└─────────────────────────────────┘
```

---

### P2-05：中文输入法支持

**当前潜在问题**

- 中文输入法的组合输入（preedit）可能不被正确处理；
- 光标位置可能与实际字符宽度不匹配；
- CJK 字符宽度计算可能有偏差。

**验证项**

```bash
# 测试中文输入
1. 打开 TUI
2. 切换到中文输入法
3. 输入 "你好世界" 观察光标位置
4. 输入 emoji "😀🎉" 观察宽度
5. 混合中英文 "你好 world" 观察对齐
```

**实现建议**

确保所有文本宽度计算使用 `get-east-asian-width` 或类似库：

```typescript
import eastAsianWidth from 'get-east-asian-width';

function displayWidth(text: string): number {
  let width = 0;
  for (const char of text) {
    width += eastAsianWidth(char) === 'fullwidth' || eastAsianWidth(char) === 'wide' ? 2 : 1;
  }
  return width;
}
```

---


---

## 四、技术约束

### 框架限制

- **OpenTUI**：当前使用 `@opentui/core` + `@opentui/solid`，需确认 Selection API 是否完整；
- **SolidJS**：响应式系统良好，但需要避免不必要的重渲染；
- **Bun**：Windows 上的 Bun 运行时可能有兼容性问题。

### 性能考虑

- 历史记录持久化应异步写入，不阻塞输入；
- 补全请求应防抖，避免频繁调用后端；
- 粘贴检测应节流，避免误判；
- 状态栏更新应与主渲染帧合并。

### 兼容性

- 需支持 Windows Terminal、PowerShell、cmd.exe；
- 需支持中文输入法（微软拼音、搜狗等）；
- 需支持不同终端尺寸（80x24 到 200x60）。

---

## 五、测试清单

### 功能测试

- [ ] Enter 发送消息
- [ ] Shift+Enter 换行
- [ ] 运行中 Enter 排队
- [ ] Agent 完成后自动发送队列
- [ ] Ctrl+K 中断
- [ ] 上下键历史记录
- [ ] 连续相同消息不重复保存
- [ ] Ctrl+R 历史搜索
- [ ] Draft 恢复
- [ ] `/` 命令补全
- [ ] `@` 文件补全
- [ ] Tab 确认补全
- [ ] Esc 关闭补全
- [ ] 粘贴大文本提示
- [ ] Ctrl+O 展开/收起粘贴
- [ ] 后端断开后可重连
- [ ] 动态底部提示

### 边界测试

- [ ] 输入框为空时 Enter
- [ ] 超长文本输入（>10000 字符）
- [ ] 粘贴超大文本（>100KB）
- [ ] 历史记录超过 50 条
- [ ] 后端反复断开重连
- [ ] 终端窗口极小（80x24）
- [ ] 中文输入法组合输入
- [ ] Emoji 输入

### 性能测试

- [ ] 输入响应延迟 < 16ms
- [ ] 补全请求防抖 120ms
- [ ] 历史记录持久化不阻塞
- [ ] 粘贴 100KB 文本不卡顿
- [ ] 长时间运行（>1 小时）无内存泄漏

---

## 六、参考实现

### dsh-TUI 关键文件

| 功能 | 文件路径 |
|------|----------|
| 选区系统 | `lib/types/ink/selection.js` |
| 剪贴板 | `lib/types/ink/termio/osc.js` |
| 选区 Hook | `lib/types/ink/hooks/use-selection.js` |
| 复制 Hook | `lib/types/ink/hooks/use-copy-on-select.js` |
| 输入组件 | `lib/types/components/PromptInput.js` |
| 补全 | `lib/types/components/CompletionMenu.js` |

### OpenTUI API

| API | 用途 |
|-----|------|
| `Clipboard.copyToClipboardOSC52()` | OSC 52 剪贴板写入 |
| `Selection.hasSelection()` | 检查是否有选区 |
| `Selection.getSelectedText()` | 获取选中文本 |
| `TextBufferRenderable.selectable` | 控制文本是否可选 |

---

## 七、附录

### A. 当前键盘快捷键完整列表

| 快捷键 | 功能 | 状态 |
|--------|------|------|
| Enter | 发送消息 | ✅ |
| Shift+Enter | 换行 | ✅ |
| Tab | 确认补全 | ✅ |
| Esc | 关闭补全/弹窗 | ✅ |
| ↑ | 上一条历史（空输入时） | ✅ |
| ↓ | 下一条历史（空输入时） | ✅ |
| Ctrl+K | 中断当前运行 | ✅ |
| Ctrl+L | 清空屏幕 | ✅ |
| Ctrl+E | 打开 reasoning effort 选择器 | ✅ |
| Ctrl+C | 不拦截（终端复制） | ✅ |
| Ctrl+Shift+C | OpenTUI 复制选区（未接入） | ⚠️ |

### B. 补全上下文规则

```typescript
// / 命令：仅在输入开头
// @ 文件：在行首或空格后
// 触发：输入变化时防抖 120ms
// 取消：光标离开上下文范围
```

### C. 文件结构

```
node_tui/
├── src-open/
│   ├── App.tsx           # 主应用（输入框、事件处理、布局）
│   ├── Welcome.tsx       # 欢迎页
│   ├── GoalView.tsx      # Goal 视图
│   ├── UsageView.tsx     # 用量统计
│   ├── theme.ts          # 主题颜色
│   ├── layout.ts         # 布局工具
│   └── sections.ts       # 消息分段
├── src/
│   ├── autocomplete.ts   # 补全逻辑
│   ├── backend.ts        # 后端通信
│   ├── state.ts          # 状态管理
│   └── types.ts          # 类型定义
└── package.json          # 依赖
```

---

*文档生成时间：2026-08-18*
*基于代码审查：node_tui/src-open/App.tsx (1267 行)*
*对比参考：dsh-TUI @deepseek-harness-tui/dsh-tui@0.8.0*
