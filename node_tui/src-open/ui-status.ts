import type {BackendConnectionState, PasteSnapshot} from './interaction.ts';

/**
 * 页脚唯一使用的派生状态。App 负责把运行时 signal 聚合成这个快照，
 * StatusLine 只负责把快照翻译成一行可读的终端状态。
 */
export type UiStatus = {
  width: number;
  backend: BackendConnectionState;
  backendExitCode?: number | null;
  running: boolean;
  phase: string;
  elapsed: string;
  currentTool?: string;
  toolsDone: number;
  toolsTotal: number;
  pending: number;
  permissionWait: boolean;
  completionOpen: boolean;
  composerLines: number;
  paste?: PasteSnapshot | null;
  toast?: string | null;
  historySearch?: {open: boolean; matches: number};
  contextUsed: number;
  contextWindow: number;
  /** contextUsed/contextWindow；窗口为零时为 0。 */
  contextUsage: number;
  /** 与 pending 相同的语义化别名，便于其他视图直接消费队列状态。 */
  queuedMessages: number;
  /** 等待权限时显示的可恢复审批提示；无需审批时为空。 */
  permissionPrompt: string | null;
  model?: string;
  effort?: string;
  spinner?: string;
};

export type UiStatusInput = Partial<UiStatus> &
  Pick<UiStatus, 'width' | 'running'> & {
    /** App 中的历史命名，允许调用方直接传入 backendState 信号。 */
    backend?: BackendConnectionState;
    backendState?: BackendConnectionState;
    /** queuedMessages 是队列信号的语义化名称，pending 仍保留为兼容别名。 */
    queuedMessages?: number;
  };

/** 将分散的运行时信号归一化为稳定、可渲染的 UI 状态。 */
export function deriveUiStatus(input: UiStatusInput): UiStatus {
  const backend = input.backendState ?? input.backend ?? 'disconnected';
  const contextUsed = Math.max(0, Number(input.contextUsed) || 0);
  const contextWindow = Math.max(0, Number(input.contextWindow) || 0);
  const queuedMessages = Math.max(0, Number(input.queuedMessages ?? input.pending) || 0);
  const permissionWait = Boolean(input.permissionWait);
  const rawExitCode = input.backendExitCode == null ? null : Number(input.backendExitCode);
  const backendExitCode = rawExitCode != null && Number.isFinite(rawExitCode) ? rawExitCode : null;

  return {
    width: Math.max(1, Number(input.width) || 1),
    backend,
    backendExitCode,
    running: Boolean(input.running),
    phase: String(input.phase || (input.running ? 'working' : 'idle')),
    elapsed: String(input.elapsed || '0s'),
    currentTool: input.currentTool ? String(input.currentTool) : undefined,
    toolsDone: Math.max(0, Number(input.toolsDone) || 0),
    toolsTotal: Math.max(0, Number(input.toolsTotal) || 0),
    pending: queuedMessages,
    queuedMessages,
    permissionWait,
    permissionPrompt: permissionWait ? 'Permission required: approve or allow to continue' : null,
    completionOpen: Boolean(input.completionOpen),
    composerLines: Math.max(1, Number(input.composerLines) || 1),
    paste: input.paste ?? null,
    toast: input.toast ?? null,
    historySearch: input.historySearch,
    contextUsed,
    contextWindow,
    contextUsage: contextWindow > 0 ? contextUsed / contextWindow : 0,
    model: input.model ? String(input.model) : undefined,
    effort: input.effort ? String(input.effort) : undefined,
    spinner: input.spinner ? String(input.spinner) : undefined,
  };
}
