import {C} from './theme.ts';
import type {UiStatus} from './ui-status.ts';

/** 将统一的 UiStatus 快照渲染为真实的终端页脚。 */
export function statusLineText(status: UiStatus): string {
  // The footer is deliberately a single quiet status row.  Model/mode/context
  // identity lives in the header; repeating it here made the composer feel
  // crowded and caused large redraws when a token arrived.
  const connection = status.backend === 'connected' ? '●' : status.backend === 'reconnecting' ? '↻' : '×';
  if (status.backend === 'reconnecting') return `${connection} reconnecting…`;
  if (status.backend === 'disconnected') {
    const code = status.backendExitCode == null || !Number.isFinite(status.backendExitCode)
      ? ''
      : ` · exit ${status.backendExitCode}`;
    return `${connection} backend unavailable${code} · Enter retry · Ctrl+R reconnect`;
  }
  if (status.permissionWait) {
    return `${connection} permission required · Allow / Deny · Esc cancel`;
  }
  if (status.completionOpen) return '⌕ suggestions · ↑↓ navigate · Tab/Enter apply · Esc close';
  if (status.historySearch?.open) {
    return `⌕ history ${status.historySearch.matches} matches · ↑↓ choose · Enter apply · Esc close`;
  }
  if (status.running) {
    const phase = status.phase || 'working';
    const tool = status.currentTool ? ` · ${status.currentTool}` : '';
    const progress = status.toolsTotal > 0 ? ` · ${status.toolsDone}/${status.toolsTotal} tools` : '';
    const queued = status.queuedMessages > 0 ? ` · q${status.queuedMessages}` : '';
    return `${connection} ${phase}${tool} · ${status.elapsed}${progress}${queued} · Enter queue · Ctrl+K interrupt`;
  }
  if (status.toast) return `✓ ${status.toast}`;
  // Keep the narrow form useful while avoiding the old multi-clause context
  // paragraph under every message.
  return status.width < 76
    ? 'Enter send · Shift+Enter newline'
    : 'Enter send · Shift+Enter newline · Ctrl+R history';
}

export function StatusLine(props: {status: UiStatus | (() => UiStatus)}): any {
  const status = () => typeof props.status === 'function' ? props.status() : props.status;
  const color = () => {
    const current = status();
    if (current.backend === 'disconnected') return C.error;
    if (current.permissionWait || current.completionOpen || current.historySearch?.open) return C.warning;
    if (current.running) return C.info;
    if (current.toast) return C.success;
    return C.textMuted;
  };
  // Footer/status content is an interaction hint, not transcript data. Keep
  // it out of mouse selection so dragging across the bottom bar never copies
  // controls instead of the conversation.
  return <box height={1} flexShrink={0} minWidth={0} paddingX={2} backgroundColor="#151b22">
    <text fg={color()} wrapMode="none" truncate selectable={false} content={statusLineText(status())} />
  </box>;
}
