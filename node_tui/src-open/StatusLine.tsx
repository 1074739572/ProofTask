import {footerHint} from './interaction.ts';
import {C} from './theme.ts';
import type {UiStatus} from './ui-status.ts';

function connectionLabel(status: UiStatus): string {
  if (status.backend === 'disconnected') return 'backend disconnected';
  if (status.backend === 'reconnecting') return 'backend reconnecting';
  return 'backend connected';
}

function contextLabel(status: UiStatus): string {
  if (!status.contextUsed && !status.contextWindow) return '';
  const used = status.contextUsed >= 1000 ? `${(status.contextUsed / 1000).toFixed(1)}k` : String(status.contextUsed);
  const window = status.contextWindow >= 1000 ? `${(status.contextWindow / 1000).toFixed(1)}k` : String(status.contextWindow);
  const ratio = status.contextWindow > 0
    ? Math.max(0, Math.min(1, status.contextUsed / status.contextWindow))
    : 0;
  return ` · ctx ${status.contextWindow > 0 ? `${used}/${window}` : used} ${Math.round(ratio * 100)}%`;
}

/** 将统一的 UiStatus 快照渲染为真实的终端页脚。 */
export function statusLineText(status: UiStatus): string {
  // 权限等待是可恢复状态，优先于普通发送提示展示审批动作。
  const hint = status.permissionWait
    ? status.permissionPrompt === 'Permission required: approve or allow to continue'
      ? '请批准后继续 · Permission approval required · choose Allow or Deny · Esc cancel'
      : status.permissionPrompt || '请批准后继续 · Permission approval required · choose Allow or Deny · Esc cancel'
    : status.running && status.backend === 'connected' && !status.completionOpen
      ? 'Enter queue · Ctrl+K interrupt'
      : footerHint(status);
  const run = status.running
    ? ` · ${status.phase || 'working'}${status.currentTool ? ` · ${status.currentTool}` : ''} · ${status.spinner || '•'} ${status.elapsed}${status.toolsTotal > 0 ? ` · ${status.toolsDone}/${status.toolsTotal} tools` : ''}`
    : '';
  const queued = status.queuedMessages > 0 ? ` · ${status.queuedMessages} queued` : '';
  return `${connectionLabel(status)}${run}${queued}${contextLabel(status)} · ${hint}`;
}

export function StatusLine(props: {status: UiStatus | (() => UiStatus)}): any {
  const status = () => typeof props.status === 'function' ? props.status() : props.status;
  const color = () => {
    const current = status();
    return current.backend === 'connected' && !current.running && !current.permissionWait ? C.textMuted : C.warning;
  };
  return <text fg={color()} wrapMode="none" truncate>{() => statusLineText(status())}</text>;
}
