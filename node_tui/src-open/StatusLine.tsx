import {C} from './theme.ts';
import type {UiStatus} from './ui-status.ts';

/** 将统一的 UiStatus 快照渲染为真实的终端页脚。 */
export function statusLineText(status: UiStatus): string {
  // The footer is deliberately a single quiet status row.  Model/mode/context
  // identity lives in the header; repeating it here made the composer feel
  // crowded and caused large redraws when a token arrived.
  const connection = status.backend === 'connected' ? '●' : status.backend === 'reconnecting' ? '↻' : '×';
  if (status.editorFullscreen) return `${connection} full-screen draft · Enter send · Esc exit editor`;
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
    const spin = status.spinner ? `${status.spinner} ` : '';
    const tool = status.currentTool ? ` · ${status.currentTool}` : '';
    const progress = status.toolsTotal > 0 ? ` · ${status.toolsDone}/${status.toolsTotal} tools` : '';
    const queued = status.queuedMessages > 0 ? ` · q${status.queuedMessages}` : '';
    const context = status.contextWindow > 0
      ? ` · ${status.width >= 100 ? contextBreakdown(status) : `ctx ${contextBar(status.contextUsage, status.width < 76 ? 6 : 9)} ${Math.round(status.contextUsage * 100)}%`}`
      : '';
    const tps = status.tokensPerSecond && status.tokensPerSecond > 0 ? ` · ${status.tokensPerSecond} t/s` : '';
    // Keep recovery/input hints before optional telemetry on compact terminals
    // so truncation never hides the action the user needs to take.
    if (status.width < 90) {
      return `${connection} ${spin}${phase}${tool} · ${status.elapsed}${progress}${queued} · Enter queue · Ctrl+K${context}${tps}`;
    }
    return `${connection} ${spin}${phase}${tool} · ${status.elapsed}${progress}${queued}${context}${tps} · Enter queue · Ctrl+K interrupt`;
  }
  if (status.toast) return `✓ ${status.toast}`;
  // Keep the narrow form useful while avoiding the old multi-clause context
  // paragraph under every message.
  return status.width < 76
    ? 'Enter send · Shift+Enter newline'
    : status.width < 100
      ? 'Enter send · Shift+Enter · /effort 推理强度'
      : 'Enter send · Shift+Enter newline · /effort 推理强度 · Ctrl+R history';
}

/** Render a compact dsh-TUI-style context meter using terminal cells. */
function contextBar(ratio: number, width: number): string {
  const safe = Math.max(0, Math.min(1, Number(ratio) || 0));
  const filled = Math.round(safe * width);
  return `${'█'.repeat(filled)}${'░'.repeat(Math.max(0, width - filled))}`;
}

/** Wide terminals get a compact legend for the four dsh-style context bands.
 * The backend currently reports aggregate context usage, so the used portion
 * is apportioned from observed tool/output activity and the remainder is free. */
function contextBreakdown(status: UiStatus): string {
  const cells = 12;
  const used = Math.round(Math.max(0, Math.min(1, status.contextUsage)) * cells);
  const free = cells - used;
  const toolShare = status.toolsTotal > 0 ? Math.min(0.4, 0.12 + status.toolsDone / status.toolsTotal * 0.2) : 0.1;
  const messageShare = status.outputTokens && status.outputTokens > 0 ? 0.55 : 0.45;
  const system = Math.max(0, used - Math.round(used * toolShare) - Math.round(used * messageShare));
  const tools = Math.max(0, Math.round(used * toolShare));
  const messages = Math.max(0, used - system - tools);
  return `ctx S${'█'.repeat(system)} T${'█'.repeat(tools)} M${'█'.repeat(messages)} F${'░'.repeat(free)} ${Math.round(status.contextUsage * 100)}%`;
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
