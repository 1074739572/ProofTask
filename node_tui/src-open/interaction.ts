import {existsSync, mkdirSync, readFileSync} from 'node:fs';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';

export const MAX_HISTORY_ITEMS = 50;
export const PASTE_DETECTION_THRESHOLD = 100;

export type PasteSnapshot = {
  text: string;
  lines: number;
  bytes: number;
  expanded: boolean;
};

export type BackendConnectionState = 'connected' | 'disconnected' | 'reconnecting';

export type FooterState = {
  width: number;
  running: boolean;
  phase: string;
  elapsed: string;
  pending: number;
  currentTool?: string;
  toolsDone: number;
  toolsTotal: number;
  backend: BackendConnectionState;
  permissionWait?: boolean;
  completionOpen?: boolean;
  composerLines: number;
  paste?: PasteSnapshot | null;
  toast?: string | null;
  historySearch?: {open: boolean; matches: number};
};

export function historyFilePath(root: string): string {
  return path.join(root, '.project', 'tui_history.json');
}

export function normalizeHistory(value: unknown, limit = MAX_HISTORY_ITEMS): string[] {
  if (!Array.isArray(value)) return [];
  const result: string[] = [];
  for (const item of value) {
    const text = String(item ?? '').trim();
    if (!text) continue;
    if (result[result.length - 1] === text) continue;
    result.push(text);
  }
  return result.slice(-Math.max(1, limit));
}

export function appendHistory(history: readonly string[], text: string, limit = MAX_HISTORY_ITEMS): string[] {
  const value = String(text || '').trim();
  if (!value) return [...history].slice(-Math.max(1, limit));
  const normalized = normalizeHistory(history, limit);
  if (normalized[normalized.length - 1] === value) return normalized;
  return [...normalized, value].slice(-Math.max(1, limit));
}

export function loadHistory(root: string, limit = MAX_HISTORY_ITEMS): string[] {
  try {
    const file = historyFilePath(root);
    if (!existsSync(file)) return [];
    return normalizeHistory(JSON.parse(readFileSync(file, 'utf8')), limit);
  } catch {
    return [];
  }
}

/** Queue writes so a fast sequence of Enter presses cannot reorder history. */
export function persistHistory(root: string, history: readonly string[]): Promise<void> {
  const file = historyFilePath(root);
  try {
    mkdirSync(path.dirname(file), {recursive: true});
  } catch {
    return Promise.resolve();
  }
  return writeFile(file, JSON.stringify(normalizeHistory(history), null, 2), 'utf8').catch(() => {});
}

export function searchHistory(history: readonly string[], query: string): string[] {
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return [...history].reverse();
  return [...history].reverse().filter(item => item.toLowerCase().includes(needle));
}

/**
 * 本地待发消息队列（P0-01）：
 * - busy 时 submit 只入队、不立即发送；
 * - setBusy(false) 是“转入空闲”的触发点，按 FIFO 顺序排空队列，每条恰好发送一次；
 * - pendingCount()/pending() 供页脚与 toast 展示本地待发数量与队列状态，
 *   不再只依赖后端的 queue_status/message_queued 事件。
 */
/**
 * Shell 风格 kill ring（P1-01 Ctrl+U/Ctrl+W/Ctrl+Y）：
 * - entries[0] 是最近一次 kill 的文本，Ctrl+Y 只取头部；
 * - lastOp 记录上一次操作：连续 kill（lastOp === 'kill'）会前插合并成单条，
 *   使 Ctrl+W、Ctrl+W、Ctrl+U 后 Ctrl+Y 能按原序还原整段文本（readline 语义）；
 * - yank 会打断累积，之后的 kill 另起新条目。
 */
export type KillRing = {entries: string[]; lastOp: 'kill' | 'yank' | null};

export function createKillRing(): KillRing {
  return {entries: [], lastOp: null};
}

export function killRingPush(ring: KillRing, killed: string): KillRing {
  const text = String(killed || '');
  if (!text) return ring;
  if (ring.lastOp === 'kill' && ring.entries.length > 0) {
    return {entries: [text + ring.entries[0], ...ring.entries.slice(1)], lastOp: 'kill'};
  }
  return {entries: [text, ...ring.entries], lastOp: 'kill'};
}

export function killRingYank(ring: KillRing): {ring: KillRing; text: string} {
  if (ring.entries.length === 0) return {ring, text: ''};
  return {ring: {...ring, lastOp: 'yank'}, text: ring.entries[0]};
}

export type MessageQueue = {
  setBusy: (busy: boolean) => void;
  submit: (command: Record<string, unknown>) => boolean;
  pendingCount: () => number;
  pending: () => Record<string, unknown>[];
};

export function createMessageQueue(send: (command: Record<string, unknown>) => boolean): MessageQueue {
  let busy = false;
  let queue: Record<string, unknown>[] = [];
  return {
    setBusy(next: boolean) {
      busy = next;
      if (busy || queue.length === 0) return;
      const flush = queue;
      queue = [];
      for (const command of flush) {
        // 发送失败的消息留在队首，等下次空闲转换再重试，避免消息丢失。
        if (!send(command)) queue.unshift(command);
      }
    },
    submit(command: Record<string, unknown>) {
      if (!busy) return send(command);
      queue.push(command);
      return true;
    },
    pendingCount: () => queue.length,
    pending: () => [...queue],
  };
}

// ---------- 结构化滚动补全菜单（P1-02 / CM1-CM3） ----------

/** 补全选项的接口形态：兼容后端返回的纯字符串与携带元数据的结构化对象。 */
export type CompletionOption = {
  label: string;
  description?: string;
  icon?: string;
  type?: string;
  isDirectory?: boolean;
};

export type CompletionOptionRow = {
  label: string;
  description: string;
  icon: string;
  directory: boolean;
};

/** 把任意补全选项（字符串或结构化对象）归一化为带安全默认值的行数据。 */
export function completionOptionRow(option: CompletionOption | string): CompletionOptionRow {
  const value: CompletionOption = typeof option === 'string' ? {label: option} : (option ?? {});
  const label = String(value.label ?? '');
  return {
    label,
    description: value.description ? String(value.description) : '',
    icon: value.icon ? String(value.icon) : '',
    directory: Boolean(value.isDirectory),
  };
}

export type CompletionWindow = {
  options: CompletionOption[];
  start: number;
  total: number;
  selected: number;
  selectedVisible: boolean;
};

/**
 * 滚动窗口：最多展示 maxRows（默认 6）行。候选不足时展示全部而不截断；
 * 超过时选中项始终落在可见区间内（跟随滚动），并返回连续切片。
 */
export function completionMenuWindow(
  options: readonly CompletionOption[],
  selected: number,
  maxRows = 6,
): CompletionWindow {
  const total = options.length;
  const rows = Math.max(1, Math.min(Math.max(1, maxRows), total));
  const index = Math.max(0, Math.min(total - 1, selected));
  const start = Math.max(0, Math.min(index - Math.floor(rows / 2), Math.max(0, total - rows)));
  const visible = options.slice(start, start + rows);
  return {
    options: visible,
    start,
    total,
    selected: index,
    selectedVisible: rows > 0 && start <= index && index < start + visible.length,
  };
}

export type DirectoryTraversal = {
  text: string;
  cursor: number;
  path: string;
};

/**
 * 接受目录补全选项：在 [start, end) 区间内用 "label/" 替换原 token（追加遍历
 * 分隔符），并返回子目录请求路径；非目录选项返回 null，不触发目录遍历。
 */
export function enterCompletionDirectory(
  option: CompletionOption | string,
  text: string,
  start: number,
  end: number,
): DirectoryTraversal | null {
  const value: CompletionOption = typeof option === 'string' ? {label: option} : (option ?? {});
  if (!value.isDirectory) return null;
  const label = String(value.label ?? '');
  const dir = label.endsWith('/') ? label : `${label}/`;
  const from = Math.max(0, Math.min(start, text.length));
  const to = Math.max(from, Math.min(end, text.length));
  return {
    text: text.slice(0, from) + dir + text.slice(to),
    cursor: from + dir.length,
    path: label.replace(/\/+$/, ''),
  };
}

export function likelyPaste(previous: string, next: string, elapsedMs: number): boolean {
  const delta = next.length - previous.length;
  if (delta < PASTE_DETECTION_THRESHOLD) return false;
  return elapsedMs <= 250 || next.split(/\r?\n/).length - previous.split(/\r?\n/).length >= 3;
}

export function makePasteSnapshot(text: string, expanded = false): PasteSnapshot {
  const value = String(text || '');
  return {
    text: value,
    lines: Math.max(1, value.split(/\r?\n/).length),
    bytes: Buffer.byteLength(value, 'utf8'),
    expanded,
  };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function foldedPasteLabel(paste: PasteSnapshot): string {
  return `[Paste: ${paste.lines} lines, ${formatBytes(paste.bytes)}]`;
}

export function footerHint(state: FooterState): string {
  if (state.backend === 'reconnecting') return 'Reconnecting backend...';
  if (state.backend === 'disconnected') return 'Backend unavailable · Enter retry · Ctrl+R reconnect';
  if (state.permissionWait) return 'Permission approval required · choose Allow or Deny · Esc cancel';
  if (state.completionOpen) return '↑↓ select · Tab/Enter apply · Esc close';
  if (state.toast && !state.running) return state.toast;
  if (state.historySearch?.open) return `History search: ${state.historySearch.matches} matches | Up/Down choose | Enter apply | Esc cancel`;
  if (state.running) {
    const tool = state.currentTool ? ` · ${state.currentTool}` : '';
    const progress = state.toolsTotal > 0 ? ` · ${state.toolsDone}/${state.toolsTotal} tools` : '';
    const queued = state.pending > 0 ? ` · ${state.pending} pending (local queue)` : '';
    return `● ${state.phase || 'working'}${tool} · ${state.elapsed}${progress}${queued} · Enter queue · Ctrl+K interrupt`;
  }
  const lines = state.composerLines > 1 ? ` · ${state.composerLines} lines` : '';
  const queued = state.pending > 0 ? ` · ${state.pending} pending (local queue)` : '';
  const paste = state.paste ? ` · ${state.paste.lines} lines pasted · Ctrl+O ${state.paste.expanded ? 'fold' : 'expand'}` : '';
  return `Enter send · Shift+Enter newline${lines}${queued}${paste}`;
}
