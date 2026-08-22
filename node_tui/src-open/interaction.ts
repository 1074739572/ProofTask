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
