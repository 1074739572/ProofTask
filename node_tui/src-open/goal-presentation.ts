import type {GoalDraftSnapshot, GoalSnapshot, GoalTaskSnapshot} from './goal-state.ts';
import {C} from './theme.ts';

/**
 * Goal 显示契约的唯一事实源：阶段词汇、状态→图标/颜色映射、任务状态判定、
 * 宽屏分栏比例、快照读取助手。GoalView / GoalSummary / GoalDetails / StatusLine
 * 全部从这里取值，避免四个文件各自硬编码后漂移。
 */

export type GoalLike = GoalSnapshot | GoalDraftSnapshot;

/** 宽屏右栏（GOAL 卡 + 任务看板 + 统计）宽度占比；左栏（执行流 + 测试 + 检查）为 1 - 该值。 */
export const GOAL_SIDE_COLUMN_RATIO = 0.36;
export const GOAL_MAIN_COLUMN_RATIO = 1 - GOAL_SIDE_COLUMN_RATIO;

export function readSource<T>(source: T | (() => T) | undefined): T | undefined {
  return typeof source === 'function' ? (source as () => T)() : source;
}

export function fallbackGoal(): GoalDraftSnapshot {
  return {
    id: 'goal',
    target: '暂无 Goal',
    status: 'ready',
    stage: 'intake',
    intake_assumptions: [],
    clarifications: [],
    question_index: 0,
    question_count: 0,
    task_count: 0,
    tasks: [],
    agents: [],
    discovery_jobs: [],
  };
}

export function isSnapshot(goal: GoalLike): goal is GoalSnapshot {
  return 'phase' in goal;
}

/** 阶段词汇表：Goal 卡片、执行链路与页脚共用同一套短词，不再各写一份。 */
export const GOAL_PHASE_LABELS: Record<string, string> = {
  intake: '需求', initialize: '初始化',
  prepare_tests: '测试准备', catalog: '测试准备', preflight: '预检',
  planning: '规划',
  discovering: '发现',
  act: '实现', working: '实现', select_task: '选择任务', prepare_execution: '执行准备',
  claim: '认领任务', rollover: '上下文重置', repair_plan: '修复规划',
  verify: '验证', verification: '验证', evaluate: '评审', clean_check: '清理检查',
  impact_review: '影响评审', full_verify: '全量回归',
  completed: '完成', done: '完成',
  paused: '已暂停', failed: '失败', cancelled: '已取消',
  // App 瞬态阶段（非 Goal 状态机取值，但页脚共用同一映射，避免回落成英文原文）
  thinking: '思考中', responding: '回复中', 'running tool': '工具执行',
  subagent: '子代理', 'goal planning': 'Goal 规划', idle: '待命',
};

/** Goal 页面 UI 文案统一表：所有硬编码中英文混排标签从这里取，避免四处漂移。 */
export const GOAL_UI_LABELS = {
  title: '目标',
  status: '状态',
  phase: '阶段',
  progress: '进度',
  rounds: '轮次',
  pipeline: '执行链路',
  taskBoard: '任务看板',
  currentTask: '当前任务',
  nextAction: '下一步',
  currentAction: '当前动作',
  executionFlow: '执行流',
  testCases: '对应测试',
  checkProcess: '模型检查过程',
  acceptances: '验收',
  command: '命令',
  evidence: '证据',
  verify: '验证',
  verifiedBy: '验证者',
  model: '模型',
  agent: 'Agent',
  task: '任务',
  context: '上下文',
  waitingPermission: '等待权限批准',
  resumeHint: '批准后可恢复',
  stale: '可能停滞',
  noEvents: '暂无模型执行事件',
  stats: '统计',
  totalElapsed: '总耗时',
  taskProgress: '任务进度',
  draftStages: {intake: '需求', discovering: '发现', planning: '规划', ready: '就绪'},
} as const;

export function goalPhaseLabel(phase: string): string {
  const text = String(phase || '').trim();
  return GOAL_PHASE_LABELS[text] || text || '准备';
}

/** 执行链路的七个展示阶段（对齐后端真实阶段机 GoalPhase）。 */
export const GOAL_TRACK: readonly [string, string][] = [
  ['intake', '需求'], ['prepare_tests', '测试准备'], ['prepare_execution', '执行准备'],
  ['act', '实现'], ['verify', '验证'], ['full_verify', '全量回归'], ['completed', '完成'],
];

/** 后端状态机阶段 → 展示轨道索引。时间线只能单调前进。 */
const PHASE_TRACK_INDEX: Record<string, number> = {
  intake: 0, initialize: 0,
  prepare_tests: 1, catalog: 1, preflight: 1,
  prepare_execution: 2, select_task: 2, claim: 2,
  act: 3, working: 3, rollover: 3, repair_plan: 3,
  verify: 4, verification: 4, evaluate: 4, clean_check: 4, impact_review: 4,
  full_verify: 5,
  completed: 6, done: 6,
};

/**
 * 阶段在时间线上的位置。paused/failed/cancelled 是 status 而非进度：
 * 优先用 resume_phase 定位暂停点；定位不到时返回 -1（不高亮任何阶段），
 * 而不是把「需求」或「验证」错标为活动阶段。
 */
export function goalPhaseTrackIndex(phase: string, resumePhase?: string | null): number {
  const direct = PHASE_TRACK_INDEX[phase];
  if (direct != null) return direct;
  if (resumePhase) {
    const resume = PHASE_TRACK_INDEX[resumePhase];
    if (resume != null) return resume;
  }
  if (phase === 'paused' || phase === 'failed' || phase === 'cancelled') return -1;
  return 0;
}

export function goalStatusLabel(status: string): string {
  switch (status) {
    case 'running': return '执行中';
    case 'paused': return '已暂停';
    case 'failed': return '失败';
    case 'permission_wait': return '已暂停（等待权限批准）';
    case 'pausing': return '正在暂停';
    case 'cancelling': return '正在取消';
    case 'cancelled': return '已取消';
    case 'ready': return '待开始';
    case 'approved': return '已批准';
    case 'completed': case 'done': return '已完成';
    default: return String(status || '').trim() || '未知状态';
  }
}

export function goalStatusIcon(status: string): string {
  if (status === 'done' || status === 'completed') return '✓';
  if (status === 'failed') return '×';
  if (status === 'cancelled') return '■';
  if (status === 'paused' || status === 'pausing' || status === 'permission_wait') return 'Ⅱ';
  return '●';
}

export function goalStatusColor(status: string): string {
  if (status === 'done' || status === 'completed') return C.success;
  if (status === 'failed') return C.error;
  if (status === 'cancelled') return C.textMuted;
  if (status === 'paused' || status === 'pausing' || status === 'permission_wait') return C.warning;
  return C.primary;
}

export type GoalTaskState = 'done' | 'failed' | 'active' | 'pending';

/** 任务状态判定：看板与详情面板共用，active 判定只有一套正则。 */
export function goalTaskState(task: GoalTaskSnapshot, currentTaskId?: string | null): GoalTaskState {
  if (task.verification_state === 'passing' || /^(done|completed|passing)$/i.test(task.status)) return 'done';
  if (/fail/i.test(task.verification_state || '') || /^(failed|error)$/i.test(task.status)) return 'failed';
  if (task.id === currentTaskId || /^(running|in_progress|active)$/i.test(task.status)) return 'active';
  return 'pending';
}

export function goalTaskIcon(state: GoalTaskState): string {
  return state === 'done' ? '✓' : state === 'failed' ? '×' : state === 'active' ? '●' : '○';
}

export function goalTaskColor(state: GoalTaskState): string {
  return state === 'done' ? C.success : state === 'failed' ? C.error : state === 'active' ? C.info : C.textMuted;
}

// ---- Draft 阶段的展示投影（GoalDraftView 使用，纯函数便于测试） ----

export type GoalTone = 'success' | 'warning' | 'error' | 'info' | 'muted';
export type GoalPresentation = {tone: GoalTone; icon: string; text: string};

export function goalDraftStageRail(draft: any): any[] {
  const stages = ['intake', 'discovering', 'planning', 'ready'];
  const idx = stages.indexOf(draft?.stage);
  return stages.map((id, i) => ({id, label: id, status: i < idx ? 'done' : i === idx ? 'active' : 'pending'}));
}

export function goalDraftNextActionPresentation(draft: any): any {
  return {tone: 'info', icon: '●', text: '继续', command: draft?.question ? '/goal answer' : null, detail: draft?.question ? '回答问题继续' : '规划完成后自动执行'};
}

export function goalDraftHeartbeatPresentation(draft: any, now = Date.now()): any {
  const heartbeat = Number(draft?.last_heartbeat || 0);
  const normalized = heartbeat > 0 && heartbeat < 1e11 ? heartbeat * 1000 : heartbeat;
  const stale = normalized > 0 && now - normalized > 30_000;
  return {tone: stale ? 'error' : 'success', icon: stale ? '×' : '✓', text: stale ? '可能停滞' : '运行正常'};
}

export function goalDraftAgentRows(draft: any): any[] {
  const labels: Record<string, string> = {architecture: '架构路径', implementation: '实现路径', tests: '测试路径', history: '历史路径'};
  return (draft?.agents || []).map((a: any) => {
    const latest = a.rounds?.[a.rounds.length - 1];
    const job = (draft?.discovery_jobs || []).find((j: any) => j.role === a.role || j.role === a.agent_type?.replace(/^goal_discovery_/, ''));
    const meta = [latest?.round ? `第 ${latest.round} 轮` : '', job?.read_path_count ? `${job.read_path_count} 个文件` : '', a.model || ''].filter(Boolean).join(' · ');
    return {label: labels[a.role] || labels[a.agent_type] || a.role || a.agent_type || 'Agent', status: a.status || 'running', activity: a.activity || a.last_text || latest?.text || '', meta};
  });
}
