import {C} from './theme.ts';
import {Sp} from './Sp.tsx';
import type {UiStatus} from './ui-status.ts';

/** 将统一的 UiStatus 快照渲染为页脚的瞬态状态行。 */
export function statusLineText(status: UiStatus): string {
  // This row carries only transient state. Identity (model/mode/effort) and
  // the context meter live in the persistent second footer row; repeating
  // them here made the composer feel crowded and caused large redraws.
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
    const tps = status.tokensPerSecond && status.tokensPerSecond > 0 ? ` · ${status.tokensPerSecond} t/s` : '';
    // Keep recovery/input hints before optional telemetry on compact terminals
    // so truncation never hides the action the user needs to take.
    if (status.width < 90) {
      return `${connection} ${spin}${phase}${tool} · ${status.elapsed}${progress}${queued} · Enter queue · Ctrl+K${tps}`;
    }
    return `${connection} ${spin}${phase}${tool} · ${status.elapsed}${progress}${queued}${tps} · Enter queue · Ctrl+K interrupt`;
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

export type ContextMeterCells = {
  used: number;
  free: number;
  percent: number;
};

/** Used/free cells for the single-fill context bar. Composition lives in the
 * click-open breakdown popup; the bar itself only shows how full the window
 * is, so its colored length always equals the reported percentage. */
export function contextMeterCells(
  status: Pick<UiStatus, 'contextUsage'>,
  cells = 12,
): ContextMeterCells {
  const usage = Math.max(0, Math.min(1, Number(status.contextUsage) || 0));
  const used = Math.round(usage * cells);
  return {used, free: cells - used, percent: Math.round(usage * 100)};
}

/** One color at a time, shifting with fullness: brand cyan while healthy,
 * amber past 60%, red past 85% (aligned with the 0.835 auto-compact line). */
export function contextMeterColor(usage: number): string {
  const value = Math.max(0, Math.min(1, Number(usage) || 0));
  if (value >= 0.85) return C.error;
  if (value >= 0.6) return C.warning;
  return C.primary;
}

function connectionOf(status: UiStatus): {icon: string; color: string} {
  if (status.backend === 'reconnecting') return {icon: '↻', color: C.warning};
  if (status.backend === 'disconnected') return {icon: '×', color: C.error};
  return {icon: '●', color: C.success};
}

/** Persistent identity row: connection, model · mode · effort (clickable),
 * and the single-fill context meter on the right. Replaces the old header
 * bar so the transcript owns every row above the composer. Clicking the
 * meter toggles the per-category breakdown popup. */
function IdentityRow(props: {status: () => UiStatus; onEffortClick?: () => void; onContextClick?: () => void}) {
  const status = () => props.status();
  const conn = () => connectionOf(status());
  const meter = () => contextMeterCells(status(), status().width >= 76 ? 12 : 8);
  const meterColor = () => contextMeterColor(status().contextUsage);
  const medium = () => status().width >= 76 && status().width < 100;
  return <box height={1} flexShrink={0} minWidth={0} paddingX={2} flexDirection="row">
    <text fg={conn().color} wrapMode="none" selectable={false}>{`${conn().icon} `}</text>
    <text fg={C.primary} wrapMode="none" selectable={false}>{status().model || 'model'}</text>
    {medium() || status().width >= 100 ? <text fg={C.textMuted} wrapMode="none" selectable={false}>{' · '}</text> : null}
    {medium() || status().width >= 100 ? <text fg={C.secondary} wrapMode="none" selectable={false}>{status().mode || 'direct'}</text> : null}
    <text fg={C.textMuted} wrapMode="none" selectable={false}>{' · '}</text>
    <box minWidth={0} flexShrink={0} onMouseUp={(event: any) => { if (event?.button === 0) props.onEffortClick?.(); }}>
      <text fg={C.textMuted} wrapMode="none" selectable={false}>{`${status().effort || 'Default'}▾`}</text>
    </box>
    <box flexGrow={1} />
    {status().contextWindow > 0 ? (
      <box minWidth={0} flexShrink={0} onMouseUp={(event: any) => { if (event?.button === 0) props.onContextClick?.(); }}>
        <text wrapMode="none" selectable={false}>
          <Sp fg={C.textMuted}>{'ctx '}</Sp>
          <Sp fg={meterColor()}>{'█'.repeat(meter().used)}</Sp>
          <Sp fg={C.textMuted}>{'░'.repeat(meter().free)}</Sp>
          <Sp fg={meterColor()}>{` ${meter().percent}%`}</Sp>
          <Sp fg={C.textMuted}>{'▾'}</Sp>
        </text>
      </box>
    ) : null}
  </box>;
}

export function StatusLine(props: {status: UiStatus | (() => UiStatus); onEffortClick?: () => void; onContextClick?: () => void}): any {
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
  return <box height={2} flexShrink={0} minWidth={0} flexDirection="column" backgroundColor="#151b22">
    <box height={1} flexShrink={0} minWidth={0} paddingX={2}>
      <text fg={color()} wrapMode="none" truncate selectable={false} content={statusLineText(status())} />
    </box>
    <IdentityRow status={status} onEffortClick={props.onEffortClick} onContextClick={props.onContextClick} />
  </box>;
}
