import {For, Show} from 'solid-js';
import {C} from './theme.ts';
export {
  goalDraftHasQuestion,
  goalDraftEventShouldFocus,
  goalDraftIsBusy,
  goalBlocksChat,
  goalEventShouldFocus,
  goalIsActive,
  goalSnapshotFromEvent,
  goalDraftSnapshotFromEvent,
  mergeGoalDiscoveryEvent,
  mergeGoalDraftAgentEvent,
  type GoalAcceptanceCase,
  type GoalAgentRoundSnapshot,
  type GoalAgentSnapshot,
  type GoalAgentToolSnapshot,
  type GoalClarification,
  type GoalDiscoveryJobSnapshot,
  type GoalDraftSnapshot,
  type GoalDraftTaskSummary,
  type GoalEvidence,
  type GoalFinalVerification,
  type GoalSnapshot,
  type GoalTaskSnapshot,
  type GoalVerificationSpec,
} from './goal-state.ts';
import {
  goalDraftHasQuestion,
  goalDraftIsBusy,
  type GoalAgentSnapshot,
  type GoalAgentToolSnapshot,
  type GoalDiscoveryJobSnapshot,
  type GoalDraftSnapshot,
  type GoalSnapshot,
  type GoalTaskSnapshot,
} from './goal-state.ts';

export type GoalDecision = {
  id: string;
  runId?: string;
  phase: string;
  agent: string;
  model?: string;
  text: string;
  status: 'active' | 'done' | 'failed';
  at: number;
  startedAt?: number;
  elapsed?: number;
  round?: number;
  tools?: Array<{id: string; name: string; summary: string; status: 'running' | 'done' | 'failed'}>;
};

export type GoalTone = 'success' | 'warning' | 'error' | 'info' | 'muted';
export type GoalPresentation = {tone: GoalTone; icon: string; text: string};
export type GoalDecisionPresentation = GoalPresentation & {title: string; owner: string; history: GoalDecision[]};

const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled']);

const PHASE_LABELS: Record<string, string> = {
  initialize: '生成任务契约',
  prepare_tests: '准备验收测试',
  select_task: '选择下一任务',
  claim: '接管任务',
  act: '实现当前任务',
  rollover: '交接执行工作者',
  verify: '运行绑定测试',
  evaluate: '独立质量评审',
  repair_plan: '制定修复方案',
  impact_review: '检查测试影响',
  clean_check: '执行清洁检查',
  full_verify: '最终全局回归',
  done: '交付完成',
  paused: '已暂停',
  cancelling: '正在取消',
  cancelled: '已取消',
  failed: '执行失败',
};

function toneColor(tone: GoalTone): string {
  if (tone === 'success') return C.success;
  if (tone === 'warning') return C.warning;
  if (tone === 'error') return C.error;
  if (tone === 'info') return C.info;
  return C.textMuted;
}

function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] || phase.replaceAll('_', ' ');
}

function verificationLabel(state: string): string {
  if (state === 'passing') return '已通过';
  if (state === 'failing') return '未通过';
  if (state === 'needs_generation') return '等待生成测试';
  if (state === 'not_started') return '尚未验证';
  if (state === 'unknown') return '状态未知';
  return state.replaceAll('_', ' ');
}

export function goalStatusPresentation(status: string): GoalPresentation {
  if (status === 'done') return {tone: 'success', icon: '✓', text: '已交付'};
  if (status === 'failed') return {tone: 'error', icon: '×', text: '失败'};
  if (status === 'cancelled') return {tone: 'muted', icon: '■', text: '已取消'};
  if (status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: '已暂停'};
  if (status === 'pausing') return {tone: 'warning', icon: 'Ⅱ', text: '正在暂停'};
  if (status === 'cancelling') return {tone: 'warning', icon: '■', text: '正在取消'};
  return {tone: 'info', icon: '●', text: '执行中'};
}

export function goalDecisionPresentation(goal: GoalSnapshot, decisions: GoalDecision[]): GoalDecisionPresentation {
  const history = decisions.slice(-4);
  if (goal.status === 'done') {
    return {tone: 'success', icon: '✓', title: 'Goal 总结', owner: '已交付', text: `${goal.tasks.filter(task => task.status === 'completed').length}/${goal.tasks.length} Task 完成，最终回归已通过`, history};
  }
  if (goal.status === 'failed') {
    return {tone: 'error', icon: '×', title: 'Goal 总结', owner: '已停止', text: goal.last_error || 'Goal 未能完成', history};
  }
  if (goal.status === 'paused') {
    return {tone: 'warning', icon: 'Ⅱ', title: '模型决策流', owner: '已暂停', text: goal.last_error || '已保存当前进度，等待恢复', history};
  }
  const active = [...decisions].reverse().find(decision => decision.status === 'active');
  if (active) {
    return {
      tone: 'info', icon: '●', title: '模型决策流', owner: active.model ? `${active.agent} · ${active.model}` : active.agent,
      text: active.text, history,
    };
  }
  return {tone: 'warning', icon: '●', title: '模型决策流', owner: phaseLabel(goal.phase), text: '正在准备下一步', history};
}

export function baselinePresentation(result?: string, source?: string): GoalPresentation {
  if (source === 'needs_generation') return {tone: 'warning', icon: '○', text: '失败基线：等待生成测试'};
  if (result === 'failing') return {tone: 'success', icon: '✓', text: '失败基线：已确认（实现前测试失败）'};
  if (source && source !== 'generated') return {tone: 'muted', icon: '·', text: '失败基线：不适用（复用已有测试）'};
  return {tone: 'warning', icon: '○', text: '失败基线：尚未确认'};
}

function taskPresentation(task: GoalTaskSnapshot, current: boolean, tasks: GoalTaskSnapshot[], goal: GoalSnapshot): GoalPresentation {
  if (current && goal.status === 'paused' && goal.stop_reason === 'permission_wait') return {tone: 'warning', icon: '!', text: '等待工具权限'};
  if (task.status === 'missing') return {tone: 'error', icon: '!', text: '状态缺失'};
  if (task.status === 'completed') return {tone: 'success', icon: '✓', text: '已完成'};
  if (current && task.verification_state === 'failing') return {tone: 'error', icon: '×', text: '测试失败，修复中'};
  if (current) return {tone: 'warning', icon: '●', text: '当前任务'};
  const incomplete = (task.blocked_by || []).filter(id => tasks.find(item => item.id === id)?.status !== 'completed');
  if (incomplete.length) return {tone: 'muted', icon: '○', text: `等待 ${incomplete.length} 个前置任务`};
  return {tone: 'muted', icon: '○', text: '等待执行'};
}

export function gatePresentation(goal: GoalSnapshot, task?: GoalTaskSnapshot): GoalPresentation {
  if (goal.status === 'paused' && goal.stop_reason === 'permission_wait') return {tone: 'warning', icon: '!', text: '等待工具权限（approval）后才能继续当前 Task'};
  if (goal.status === 'paused' && goal.stop_reason === 'impact_review_format_error') return {tone: 'warning', icon: '!', text: '影响审查输出无效，自动重试后已暂停'};
  if (goal.status === 'done') return {tone: 'success', icon: '✓', text: '全部 Task 证据与最终回归均已通过，可以交付'};
  if (goal.status === 'failed') return {tone: 'error', icon: '×', text: '机器门禁未通过，Goal 已停止'};
  if (goal.status === 'cancelled') return {tone: 'muted', icon: '■', text: 'Goal 已取消，现有证据保留'};
  if (goal.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: 'Goal 已暂停，可以恢复后继续验证'};
  if (goal.status === 'pausing') return {tone: 'warning', icon: 'Ⅱ', text: '正在等待当前操作到达暂停点'};
  if (goal.status === 'cancelling') return {tone: 'warning', icon: '■', text: '正在等待当前操作安全停止'};
  if (!task) return {tone: 'info', icon: '●', text: '正在生成可验证的 Task 契约'};
  if (goal.phase === 'prepare_tests') return {tone: 'warning', icon: '○', text: '等待测试生成、收集和失败基线确认'};
  if (goal.phase === 'verify') return {tone: 'warning', icon: '●', text: '正在等待绑定测试的机器结果'};
  if (goal.phase === 'evaluate') return {tone: 'info', icon: '●', text: '测试已通过，正在进行独立质量评审'};
  if (goal.phase === 'clean_check') return {tone: 'warning', icon: '●', text: '测试已通过，正在执行严格 clean check'};
  if (goal.phase === 'full_verify') return {tone: 'warning', icon: '●', text: '正在重跑全部 Task 测试和全局回归'};
  if (task.verification_state === 'failing') return {tone: 'error', icon: '×', text: '上一次机器测试失败，模型正在修复当前 Task'};
  if (task.verification_state === 'passing') return {tone: 'info', icon: '✓', text: '绑定测试已通过，仍需完成后续交付门禁'};
  return {tone: 'warning', icon: '○', text: 'Task 尚未取得零退出码测试证据'};
}

function sourceLabel(source?: string): string {
  if (source === 'generated') return '生成测试';
  if (source === 'discovered') return '已有测试目录';
  if (source === 'user') return '用户指定';
  if (source === 'needs_generation') return '等待生成';
  return source || '未知来源';
}

function formatDuration(duration?: number | null): string {
  if (duration == null || !Number.isFinite(duration)) return '-';
  if (duration < 1000) return `${Math.round(duration)} ms`;
  return `${(duration / 1000).toFixed(1)} s`;
}

function evidenceLines(value?: string, limit = 5): string[] {
  const clean = String(value || '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
  return clean.split(/\r?\n/).map(line => line.trimEnd()).filter(Boolean).slice(-limit);
}

function progressBar(completed: number, total: number, width: number): string {
  const filled = total > 0 ? Math.round((completed / total) * width) : 0;
  return `${'█'.repeat(filled)}${'░'.repeat(Math.max(0, width - filled))}`;
}

export function goalRegressionPresentation(goal: GoalSnapshot): GoalPresentation {
  const final = goal.final_verification;
  if (final?.status === 'passed' && final.exit_code === 0) return {tone: 'success', icon: '✓', text: `通过 · exit 0 · ${formatDuration(final.duration_ms)}`};
  if (final?.status === 'failed') return {tone: 'error', icon: '×', text: `失败 · 退出码 ${final.exit_code ?? '-'}`};
  if (final?.status === 'blocked') return {tone: 'error', icon: '×', text: '未运行：Task 最终绑定未通过'};
  if (final?.status === 'running') return {tone: 'warning', icon: '●', text: '正在运行'};
  if (goal.status === 'done') return {tone: 'success', icon: '✓', text: '状态机结果：通过'};
  if (goal.status === 'failed' && goal.stop_reason === 'full_verification_failed') return {tone: 'error', icon: '×', text: '状态机结果：失败'};
  if (goal.phase === 'full_verify') return {tone: 'warning', icon: '●', text: '状态机结果：运行中'};
  if (goal.status === 'cancelled') return {tone: 'muted', icon: '■', text: '状态机结果：未完成'};
  return {tone: 'muted', icon: '○', text: '状态机结果：等待全部 Task 完成'};
}

function epochMs(value?: number | null): number {
  if (!value || !Number.isFinite(value)) return 0;
  return value < 10_000_000_000 ? value * 1000 : value;
}

function formatElapsedSeconds(value: number): string {
  const seconds = Math.max(0, Math.floor(value));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

export type GoalNextAction = GoalPresentation & {command: string; detail: string};

export function goalNextActionPresentation(goal: GoalSnapshot): GoalNextAction {
  if (goal.status === 'paused' && goal.stop_reason === 'user_approval_required') {
    return {tone: 'warning', icon: '▶', text: '等待执行批准', command: '/goal run', detail: '测试准备已完成；确认测试后允许写入实现。'};
  }
  if (goal.status === 'paused' && goal.stop_reason === 'permission_wait') {
    return {tone: 'warning', icon: '!', text: '等待工具权限', command: '先处理权限，再输入 /goal resume', detail: '当前 Task 没有越过权限边界，证据仍已保存。'};
  }
  if (goal.status === 'paused') {
    return {tone: 'warning', icon: 'Ⅱ', text: 'Goal 已暂停', command: '/goal resume', detail: goal.last_error || '从保存的 resume phase 继续。'};
  }
  if (goal.status === 'pausing' || goal.status === 'cancelling') {
    return {tone: 'warning', icon: '…', text: goal.status === 'pausing' ? '正在暂停' : '正在取消', command: '/goal status', detail: '当前操作到达安全检查点后会更新最终状态。'};
  }
  if (goal.status === 'done') {
    return {tone: 'success', icon: '✓', text: 'Goal 已交付', command: '开始新的 /goal', detail: '最终回归和所有 Task 证据已保留。'};
  }
  if (goal.status === 'cancelled') {
    return {tone: 'muted', icon: '■', text: 'Goal 已取消', command: '开始新的 /goal', detail: '历史和已生成文件保留，当前 Goal 不会自动恢复。'};
  }
  if (goal.status === 'failed') {
    return {tone: 'error', icon: '×', text: 'Goal 已失败', command: '/goal status', detail: goal.last_error || '检查错误后决定是新建 Goal 还是修复环境。'};
  }
  return {tone: 'info', icon: '●', text: 'Goal 执行中', command: '/goal pause', detail: `${phaseLabel(goal.phase)} 正在推进；需要聊天时先暂停 Goal。`};
}

export type GoalDraftPresentation = GoalPresentation & {command: string; detail: string};

export function goalDraftStagePresentation(draft: GoalDraftSnapshot): GoalPresentation {
  if (draft.status === 'ready') return {tone: 'success', icon: '✓', text: '草案已准备好'};
  if (draft.status === 'clarifying') return {tone: 'warning', icon: '?', text: '等待你的确认'};
  if (draft.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: '草案已暂停'};
  if (draft.status === 'cancelled' || draft.event === 'discarded') return {tone: 'muted', icon: '■', text: '草案已取消'};
  if (draft.status === 'failed') return {tone: 'error', icon: '×', text: '草案遇到错误'};
  if (draft.status === 'consumed') return {tone: 'muted', icon: '✓', text: '草案已交给 Goal'};
  const labels: Record<string, string> = {
    preflight: '检查执行条件', catalog: '收集测试目录', intake: '判断需求清晰度',
    discovering: '只读检查仓库', planning: '生成 Task 草案', clarifying: '等待确认',
  };
  return {tone: 'info', icon: '●', text: labels[draft.stage] || `正在${draft.stage}`};
}

export function goalDraftNextActionPresentation(draft: GoalDraftSnapshot): GoalDraftPresentation {
  if (draft.status === 'clarifying' && draft.question) {
    return {tone: 'warning', icon: '?', text: '需要你的回答', command: '/goal answer <回答>', detail: '也可以直接在输入框自然回答当前问题。'};
  }
  if (draft.status === 'ready') {
    return {tone: 'success', icon: '▶', text: '等待批准执行', command: '/goal approve', detail: '批准后才会生成测试或修改实现文件。'};
  }
  if (draft.status === 'paused') {
    return {tone: 'warning', icon: 'Ⅱ', text: '等待恢复', command: '/goal resume', detail: draft.last_error || '恢复后会从保存的阶段继续。'};
  }
  if (draft.status === 'cancelled' || draft.status === 'consumed') {
    return {tone: 'muted', icon: '■', text: '流程已结束', command: '开始新的 /goal', detail: '当前草案不会继续运行。'};
  }
  return {tone: 'info', icon: '●', text: '后台处理中', command: '/goal pause', detail: '需要介入时可暂停；当前只读阶段不会写入实现。'};
}

type GoalRailStatus = 'done' | 'active' | 'pending' | 'blocked';
export type GoalStageRailItem = {id: string; label: string; status: GoalRailStatus};

const DRAFT_STAGES = [
  {id: 'preflight', label: '受理'},
  {id: 'catalog', label: '测试目录'},
  {id: 'intake', label: '需求确认'},
  {id: 'discovering', label: '仓库探索'},
  {id: 'planning', label: '任务规划'},
  {id: 'ready', label: '待批准'},
] as const;

function effectiveDraftStage(draft: GoalDraftSnapshot): string {
  if (draft.stage === 'clarifying') return 'intake';
  if (DRAFT_STAGES.some(item => item.id === draft.stage)) return draft.stage;
  const lastAgent = [...(draft.agents || [])].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))[0];
  if (lastAgent?.stage && DRAFT_STAGES.some(item => item.id === lastAgent.stage)) return lastAgent.stage;
  if (draft.discovery_jobs.length) return 'discovering';
  if (draft.intake_summary) return 'intake';
  return 'preflight';
}

export function goalDraftStageRail(draft: GoalDraftSnapshot): GoalStageRailItem[] {
  const current = draft.status === 'ready' || draft.status === 'approved' ? 'ready' : effectiveDraftStage(draft);
  const currentIndex = Math.max(0, DRAFT_STAGES.findIndex(item => item.id === current));
  const blocked = draft.status === 'paused' || draft.status === 'failed' || draft.status === 'cancelled';
  return DRAFT_STAGES.map((item, index) => ({
    ...item,
    status: index < currentIndex ? 'done' : index === currentIndex ? (blocked ? 'blocked' : 'active') : 'pending',
  }));
}

export type GoalHeartbeatPresentation = GoalPresentation & {elapsed: string; deadline: string};

export function goalDraftHeartbeatPresentation(draft: GoalDraftSnapshot, now = Date.now()): GoalHeartbeatPresentation {
  const stageStarted = epochMs(draft.stage_started_at);
  const heartbeat = epochMs(draft.last_heartbeat || draft.updated_at);
  const deadline = epochMs(draft.stage_deadline);
  const elapsedSeconds = stageStarted ? (now - stageStarted) / 1000 : 0;
  const heartbeatAge = heartbeat ? Math.max(0, (now - heartbeat) / 1000) : Number.POSITIVE_INFINITY;
  const busy = goalDraftIsBusy(draft);
  let tone: GoalTone = 'muted';
  let icon = '○';
  let text = '等待阶段更新';
  if (busy && heartbeatAge <= 10) {
    tone = 'success'; icon = '●'; text = `心跳正常 · ${formatElapsedSeconds(heartbeatAge)} 前更新`;
  } else if (busy && heartbeatAge <= 30) {
    tone = 'warning'; icon = '!'; text = `${formatElapsedSeconds(heartbeatAge)} 没有新状态`;
  } else if (busy) {
    tone = 'error'; icon = '×'; text = heartbeat ? `可能停滞 · ${formatElapsedSeconds(heartbeatAge)} 没有心跳` : '等待首次心跳';
  } else if (heartbeat) {
    text = `${formatElapsedSeconds(heartbeatAge)} 前更新`;
  }
  const deadlineText = deadline
    ? (deadline > now ? `剩余 ${formatElapsedSeconds((deadline - now) / 1000)}` : '已超过阶段时限')
    : '无阶段时限';
  return {tone, icon, text, elapsed: formatElapsedSeconds(elapsedSeconds), deadline: deadlineText};
}

const DRAFT_AGENT_LABELS: Record<string, string> = {
  goal_intake: '需求判断',
  goal_planner: 'Task 规划',
  goal_test_writer: '验收测试',
  goal_discovery_requirement: '需求边界',
  goal_discovery_architecture: '架构路径',
  goal_discovery_implementation: '实现定位',
  goal_discovery_tests: '测试覆盖',
  goal_discovery_history: '历史约束',
};

type GoalAgentRowStatus = 'queued' | 'running' | 'validating' | 'done' | 'failed';
export type GoalAgentRow = {
  id: string;
  label: string;
  agentType: string;
  stage: string;
  status: GoalAgentRowStatus;
  activity: string;
  meta: string;
  elapsed: string;
  tools: GoalAgentToolSnapshot[];
  error?: string;
  updatedAt: number;
};

function normalizedAgentStatus(status: string): GoalAgentRowStatus {
  if (status === 'done' || status === 'completed') return 'done';
  if (status === 'failed' || status === 'timeout' || status === 'cancelled') return 'failed';
  if (status === 'running' || status === 'started') return 'running';
  return 'queued';
}

function agentRowPresentation(status: GoalAgentRowStatus): GoalPresentation {
  if (status === 'done') return {tone: 'success', icon: '✓', text: '已完成'};
  if (status === 'failed') return {tone: 'error', icon: '×', text: '失败'};
  if (status === 'running') return {tone: 'info', icon: '●', text: '工作中'};
  if (status === 'validating') return {tone: 'warning', icon: '◆', text: '整理证据'};
  return {tone: 'muted', icon: '○', text: '等待并发槽'};
}

function stageName(stage: string): string {
  return DRAFT_STAGES.find(item => item.id === stage)?.label || stage.replaceAll('_', ' ');
}

function draftAgentLabel(agentType: string, role: string): string {
  return DRAFT_AGENT_LABELS[agentType] || DRAFT_AGENT_LABELS[`goal_discovery_${role}`] || role.replaceAll('_', ' ');
}

function elapsedForAgent(agent: GoalAgentSnapshot | undefined, job: GoalDiscoveryJobSnapshot | undefined, now: number): string {
  if (agent?.elapsed != null && agent.status !== 'running') return formatElapsedSeconds(agent.elapsed);
  const start = epochMs(agent?.started_at || job?.started_at);
  const finish = epochMs(agent?.finished_at || job?.finished_at);
  return start ? formatElapsedSeconds(((finish || now) - start) / 1000) : '-';
}

function agentActivity(agent: GoalAgentSnapshot, label: string): string {
  if (agent.status !== 'running' && agent.summary) return agent.summary;
  const latestRound = agent.rounds[agent.rounds.length - 1];
  if (latestRound?.text) return latestRound.text;
  if (/^discover\s.+\sevidence$/i.test(agent.description)) return `正在检查${label}相关文件并形成可引用证据`;
  return agent.description || '正在准备下一步';
}

export function goalDraftAgentRows(draft: GoalDraftSnapshot, now = Date.now()): GoalAgentRow[] {
  const agents = draft.agents || [];
  const usedRoles = new Set<string>();
  const rows: GoalAgentRow[] = agents.map(agent => {
    const job = [...draft.discovery_jobs].reverse().find(item => item.role === agent.role);
    if (job) usedRoles.add(job.role);
    const jobStatus = job ? normalizedAgentStatus(job.status) : undefined;
    const status: GoalAgentRowStatus = agent.status === 'done' && jobStatus === 'running'
      ? 'validating'
      : agent.status === 'failed' ? 'failed' : agent.status === 'done' ? 'done' : 'running';
    const round = agent.rounds[agent.rounds.length - 1]?.round;
    const meta = [
      stageName(agent.stage),
      agent.model || 'model pending',
      round ? `第 ${round} 轮` : '',
      `${Math.max(agent.tool_count, agent.tools.length)} 次工具`,
      job?.read_path_count ? `${job.read_path_count} 个文件` : '',
    ].filter(Boolean).join(' · ');
    return {
      id: agent.id,
      label: draftAgentLabel(agent.agent_type, agent.role),
      agentType: agent.agent_type,
      stage: agent.stage,
      status,
      activity: agentActivity(agent, draftAgentLabel(agent.agent_type, agent.role)),
      meta,
      elapsed: elapsedForAgent(agent, job, now),
      tools: agent.tools.slice(-2),
      error: job?.error,
      updatedAt: epochMs(agent.updated_at) || epochMs(job?.event_ts),
    };
  });
  for (const job of draft.discovery_jobs) {
    if (usedRoles.has(job.role)) continue;
    const status = normalizedAgentStatus(job.status);
    const activity = status === 'queued' ? '已分配读取范围，等待可用并发槽'
      : status === 'running' ? '正在读取分配文件并整理结构化证据'
      : status === 'done' ? '证据报告已完成并交回规划器'
      : job.error || '证据任务未完成';
    rows.push({
      id: job.id,
      label: draftAgentLabel(`goal_discovery_${job.role}`, job.role),
      agentType: `goal_discovery_${job.role}`,
      stage: 'discovering',
      status,
      activity,
      meta: ['仓库探索', `${job.read_path_count} 个文件`, `工具 ${job.tools.join(', ') || 'read_file'}`].join(' · '),
      elapsed: elapsedForAgent(undefined, job, now),
      tools: [],
      error: job.error,
      updatedAt: epochMs(job.event_ts),
    });
  }
  if (!rows.length) {
    const stage = effectiveDraftStage(draft);
    const synthetic: Record<string, [string, string, string]> = {
      preflight: ['运行协调器', 'system/preflight', '正在检查 Agent 路由、权限与验证条件'],
      catalog: ['测试目录扫描', 'system/test-catalog', '正在收集真实测试 selector 和验证适配器'],
      intake: ['需求判断', 'goal_intake', goalDraftHasQuestion(draft) ? '已识别需要你决定的范围问题' : '正在判断哪些问题必须由你确认'],
      discovering: ['探索调度器', 'system/discovery', '正在切分仓库范围并分配只读证据任务'],
      planning: ['Task 规划', 'goal_planner', '正在把仓库证据整理成可验证 Task'],
      ready: ['运行协调器', 'system/approval', '草案已就绪，等待执行批准'],
    };
    const value = synthetic[stage] || synthetic.preflight;
    rows.push({
      id: `system-${stage}`, label: value[0], agentType: value[1], stage,
      status: goalDraftIsBusy(draft) ? 'running' : draft.status === 'failed' ? 'failed' : 'done',
      activity: value[2], meta: `${stageName(stage)} · 系统任务`, elapsed: goalDraftHeartbeatPresentation(draft, now).elapsed,
      tools: [], error: draft.last_error, updatedAt: epochMs(draft.updated_at),
    });
  }
  const rank: Record<GoalAgentRowStatus, number> = {running: 0, validating: 1, failed: 2, queued: 3, done: 4};
  return rows.sort((a, b) => rank[a.status] - rank[b.status] || b.updatedAt - a.updatedAt);
}

function StageRail(props: {items: GoalStageRailItem[]; compact: boolean}) {
  const groups = () => props.compact ? [props.items.slice(0, 3), props.items.slice(3)] : [props.items];
  const presentation = (status: GoalRailStatus): GoalPresentation => {
    if (status === 'done') return {tone: 'success', icon: '✓', text: ''};
    if (status === 'active') return {tone: 'info', icon: '●', text: ''};
    if (status === 'blocked') return {tone: 'warning', icon: '!', text: ''};
    return {tone: 'muted', icon: '○', text: ''};
  };
  return <box flexDirection="column" minWidth={0}>
    <For each={groups()}>{group => <box flexDirection="row" minWidth={0}>
      <For each={group}>{(item, index) => <>
        <text fg={toneColor(presentation(item.status).tone)} wrapMode="none">{presentation(item.status).icon} {item.label}</text>
        <Show when={index() < group.length - 1}><text fg={C.textMuted} wrapMode="none">  ─  </text></Show>
      </>}</For>
    </box>}</For>
  </box>;
}

export function GoalDraftView(props: {draft: GoalDraftSnapshot; now?: number; width: number; height: number}) {
  const compact = () => props.width < 82;
  const status = () => goalDraftStagePresentation(props.draft);
  const next = () => goalDraftNextActionPresentation(props.draft);
  const activeQuestion = () => goalDraftHasQuestion(props.draft);
  const busy = () => goalDraftIsBusy(props.draft);
  const heartbeat = () => goalDraftHeartbeatPresentation(props.draft, props.now || Date.now());
  const rows = () => goalDraftAgentRows(props.draft, props.now || Date.now());
  const activeCount = () => rows().filter(row => row.status === 'running' || row.status === 'validating').length;
  const doneCount = () => rows().filter(row => row.status === 'done').length;
  const nextPanel = () => <box border borderStyle="rounded" borderColor={toneColor(next().tone)} paddingX={1} flexDirection="column" minWidth={0}>
    <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
      <text fg={toneColor(next().tone)}>{next().icon} {next().text}</text>
      <text fg={C.primary} wrapMode="none">{next().command}</text>
    </box>
    <text fg={C.textMuted} wrapMode="word">{next().detail}</text>
  </box>;
  return <scrollbox height={props.height} flexShrink={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
    <box flexDirection="column" paddingX={1} paddingTop={1} paddingBottom={1}>
      <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
        <text fg={toneColor(status().tone)}>{status().icon} GOAL CONTROL · {status().text}</text>
        <text fg={C.warning}>{stageName(effectiveDraftStage(props.draft))} · {heartbeat().elapsed}</text>
      </box>
      <text fg={C.text} wrapMode="word">{props.draft.target}</text>
      <text fg={C.textMuted} wrapMode="none" truncate>{props.draft.id}</text>

      <box flexDirection="column" minWidth={0}>
        <text fg={C.secondary}>阶段位置</text>
        <StageRail items={goalDraftStageRail(props.draft)} compact={props.width < 96} />
        <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
          <text fg={toneColor(heartbeat().tone)} wrapMode="none" truncate>{heartbeat().icon} {heartbeat().text}</text>
          <text fg={heartbeat().deadline.startsWith('已超过') ? C.error : C.textMuted} wrapMode="none">{heartbeat().deadline}</text>
        </box>
      </box>

      <Show when={!busy()}>{nextPanel()}</Show>

      <box border borderStyle="rounded" borderColor={activeCount() ? C.info : C.textMuted} paddingX={1} flexDirection="column" minWidth={0}>
        <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
          <text fg={C.secondary}>AGENT 现场</text>
          <text fg={C.textMuted}>{activeCount()} 工作中 · {doneCount()} 已完成 · {rows().length} 总数</text>
        </box>
        <For each={rows()}>{row => {
          const presentation = () => agentRowPresentation(row.status);
          return <box flexDirection="column" minWidth={0}>
            <box flexDirection="row" justifyContent="space-between" minWidth={0}>
              <text fg={toneColor(presentation().tone)} wrapMode="none" truncate flexGrow={1}>{presentation().icon} {row.label} · {row.agentType}</text>
              <text fg={toneColor(presentation().tone)} wrapMode="none">{presentation().text} · {row.elapsed}</text>
            </box>
            <text fg={row.status === 'running' ? C.text : C.textMuted} wrapMode="word">  {row.activity}</text>
            <text fg={C.textMuted} wrapMode="none" truncate>  └ {row.meta}</text>
            <Show when={row.status === 'running' || row.status === 'validating'}>
              <For each={row.tools}>{tool => <text fg={tool.status === 'failed' ? C.error : tool.status === 'running' ? C.info : C.textMuted} wrapMode="none" truncate>
                {'    '}{tool.status === 'running' ? '●' : tool.status === 'failed' ? '×' : '✓'} {tool.name}{tool.summary ? ` · ${tool.summary}` : ''}
              </text>}</For>
            </Show>
            <Show when={row.error}><text fg={C.error} wrapMode="word">  错误：{row.error}</text></Show>
          </box>;
        }}</For>
        <Show when={props.draft.discovery_path}>
          <text fg={C.textMuted} wrapMode="none" truncate>证据输出 · {props.draft.discovery_path}</text>
        </Show>
      </box>

      <Show when={busy()}>
        <text fg={C.textMuted} wrapMode="word">需要介入时输入 /goal pause；当前阶段不会修改实现文件。</text>
      </Show>

      <Show when={props.draft.intake_summary || props.draft.intake_assumptions.length || props.draft.clarifications.length || props.draft.question}>
        <box border borderStyle="rounded" borderColor={C.textMuted} paddingX={1} flexDirection="column" minWidth={0}>
          <text fg={C.secondary}>阶段产物 · Intake</text>
          <text fg={C.text} wrapMode="word">{props.draft.intake_summary || (activeQuestion() ? '发现了需要你决定的范围问题。' : '正在等待 intake 结论。')}</text>
          <For each={props.draft.intake_assumptions}>{item => <text fg={C.textMuted} wrapMode="word">· 假设：{item}</text>}</For>
          <For each={props.draft.clarifications}>{item => <text fg={C.textMuted} wrapMode="word">✓ {item.question} → {item.answer}</text>}</For>
          <Show when={props.draft.question}>
            <text fg={C.warning} wrapMode="word">问题 {props.draft.question_index + 1}/{Math.max(1, props.draft.question_count)}：{props.draft.question}</text>
          </Show>
        </box>
      </Show>

      <Show when={props.draft.tasks.length || props.draft.status === 'planning'}>
        <box border borderStyle="rounded" borderColor={C.secondary} paddingX={1} flexDirection="column" minWidth={0}>
          <text fg={C.secondary}>阶段产物 · Task 草案 · {props.draft.tasks.length || props.draft.task_count}</text>
          <Show when={!props.draft.tasks.length}><text fg={C.textMuted}>规划模型正在把证据整理成可验证 Task…</text></Show>
          <For each={props.draft.tasks}>{(item, index) => <box flexDirection="column" minWidth={0}>
            <text fg={C.text} wrapMode="word">{String(index() + 1).padStart(2, '0')} · {item.name}</text>
            <text fg={C.textMuted} wrapMode="word">  {item.behavior || '未提供行为摘要'}</text>
            <text fg={C.textMuted} wrapMode="word">  验收 {item.acceptance_count || 0} 条 · {item.verification_source || 'needs_generation'}{item.selectors?.length ? ` · ${item.selectors.join(', ')}` : ''}</text>
          </box>}</For>
        </box>
      </Show>

      <Show when={props.draft.last_error}><text fg={C.error} wrapMode="word">最近错误：{props.draft.last_error}</text></Show>
      <Show when={props.draft.message}><text fg={C.textMuted} wrapMode="word">进度：{props.draft.message}</text></Show>
    </box>
  </scrollbox>;
}

const EXECUTION_STAGES = [
  {id: 'contract', label: '任务合同', phases: ['initialize', 'select_task', 'claim']},
  {id: 'tests', label: '测试准备', phases: ['prepare_tests']},
  {id: 'act', label: '实现', phases: ['act', 'rollover']},
  {id: 'verify', label: '验证', phases: ['verify']},
  {id: 'review', label: '评审返修', phases: ['evaluate', 'repair_plan', 'impact_review']},
  {id: 'clean', label: '清洁检查', phases: ['clean_check']},
  {id: 'regression', label: '全局回归', phases: ['full_verify', 'done']},
] as const;

export function goalExecutionStageRail(goal: GoalSnapshot): GoalStageRailItem[] {
  const phase = goal.phase === 'paused' && goal.resume_phase ? goal.resume_phase : goal.phase;
  let currentIndex = EXECUTION_STAGES.findIndex(item => (item.phases as readonly string[]).includes(phase));
  if (goal.status === 'done') currentIndex = EXECUTION_STAGES.length - 1;
  if (currentIndex < 0) currentIndex = 0;
  const blocked = goal.status === 'paused' || goal.status === 'failed' || goal.status === 'cancelled';
  return EXECUTION_STAGES.map((item, index) => ({
    id: item.id,
    label: item.label,
    status: goal.status === 'done' || index < currentIndex ? 'done' : index === currentIndex ? (blocked ? 'blocked' : 'active') : 'pending',
  }));
}

export function GoalView(props: {goal: GoalSnapshot; decisions?: GoalDecision[]; now?: number; width: number; height: number}) {
  const compact = () => props.width < 82;
  const current = () => props.goal.tasks.find(task => task.id === props.goal.current_task_id);
  const completed = () => props.goal.tasks.filter(task => task.status === 'completed').length;
  const status = () => goalStatusPresentation(props.goal.status);
  const gate = () => gatePresentation(props.goal, current());
  const latest = () => current()?.latest_evidence;
  const output = () => evidenceLines(latest()?.stdout_tail, compact() ? 3 : 5);
  const regression = () => goalRegressionPresentation(props.goal);
  const finalOutput = () => evidenceLines(props.goal.final_verification?.stdout_tail, compact() ? 2 : 3);
  const terminal = () => TERMINAL_STATUSES.has(props.goal.status);
  const decisions = () => props.decisions || [];
  const decision = () => goalDecisionPresentation(props.goal, decisions());
  const activeDecision = () => [...decisions()].reverse().find(item => item.status === 'active');
  const decisionHistory = () => decision().history.filter(item => item.status !== 'active').slice(-2);
  const activeTools = () => (activeDecision()?.tools || []).slice(-2);
  const activeElapsed = () => {
    const active = activeDecision();
    if (!active) return '';
    const seconds = active.elapsed ?? ((props.now || Date.now()) - (active.startedAt || active.at)) / 1000;
    return formatElapsedSeconds(seconds);
  };
  const decisionPulse = () => ['●', '◌', '○'][Math.floor((props.now || 0) / 520) % 3];
  const next = () => goalNextActionPresentation(props.goal);
  const nextPanel = () => <box border borderStyle="rounded" borderColor={toneColor(next().tone)} paddingX={1} flexDirection="column" minWidth={0}>
    <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
      <text fg={toneColor(next().tone)}>{next().icon} {next().text}</text>
      <text fg={C.primary} wrapMode="none">{next().command}</text>
    </box>
    <text fg={C.textMuted} wrapMode="word">{next().detail}</text>
  </box>;
  const gatePanel = () => <box border borderStyle="rounded" borderColor={toneColor(gate().tone)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
    <text fg={toneColor(gate().tone)}>机器门禁</text>
    <text fg={C.text} wrapMode="word">{gate().icon} {gate().text}</text>
    <Show when={(props.goal.last_error || current()?.last_error) && props.goal.stop_reason !== 'permission_wait'}>
      <text fg={C.error} wrapMode="word">最近错误：{props.goal.last_error || current()?.last_error}</text>
    </Show>
    <Show when={props.goal.stop_reason === 'permission_wait'}>
      <text fg={C.warning} wrapMode="word">等待工具权限后输入 /goal resume</text>
    </Show>
    <Show when={props.goal.stop_reason}>
      <text fg={C.textMuted} wrapMode="word">停止原因：{props.goal.stop_reason}</text>
    </Show>
  </box>;
  const regressionPanel = () => <box border borderStyle="rounded" borderColor={toneColor(regression().tone)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
    <text fg={C.secondary}>Goal 最终回归</text>
    <text fg={C.info} wrapMode="word">{props.goal.verification || '未配置全局回归命令'}</text>
    <text fg={toneColor(regression().tone)}>{regression().icon} {regression().text}</text>
    <Show when={props.goal.final_verification?.code_snapshot}>
      <text fg={C.textMuted} wrapMode="none" truncate>代码快照 {props.goal.final_verification?.code_snapshot}</text>
    </Show>
    <For each={finalOutput()}>{line => <text fg={C.textMuted} wrapMode="word">  {line}</text>}</For>
    <Show when={props.goal.final_verification?.error}>
      <text fg={C.error} wrapMode="word">{props.goal.final_verification?.error}</text>
    </Show>
  </box>;

  return <scrollbox height={props.height} flexShrink={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
    <box flexDirection="column" paddingX={1} paddingTop={1} paddingBottom={1}>
      <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
        <text fg={toneColor(status().tone)}>{status().icon} GOAL · {status().text}</text>
        <text fg={C.warning}>{phaseLabel(props.goal.phase)}</text>
      </box>
      <text fg={C.text} wrapMode="word">{props.goal.target}</text>
      <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={compact() ? 0 : 1}>
        <text fg={C.success} wrapMode="none">{progressBar(completed(), props.goal.tasks.length, compact() ? 10 : 18)}</text>
        <text fg={C.textMuted} wrapMode="none">{completed()}/{props.goal.tasks.length} Tasks</text>
        <text fg={C.textMuted} wrapMode="none">· 模型轮次 {props.goal.total_llm_rounds || 0}</text>
        <Show when={(props.goal.worker_rollovers || 0) > 0}>
          <text fg={C.textMuted} wrapMode="none">· 已交接 {props.goal.worker_rollovers} 次</text>
        </Show>
      </box>
      <text fg={C.textMuted} wrapMode="none" truncate>{props.goal.id}</text>

      <box flexDirection="column" minWidth={0}>
        <text fg={C.secondary}>执行链路</text>
        <StageRail items={goalExecutionStageRail(props.goal)} compact={props.width < 112} />
      </box>

      <Show when={props.goal.status !== 'running'}>{nextPanel()}</Show>

      <box border borderStyle="rounded" borderColor={toneColor(decision().tone)} paddingX={1} flexDirection="column" minWidth={0} height={compact() ? 9 : 8} flexShrink={0}>
        <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
          <text fg={toneColor(decision().tone)}>AGENT 现场</text>
          <text fg={C.textMuted} wrapMode="none" truncate>{decision().owner}{activeElapsed() ? ` · ${activeElapsed()}` : ''}</text>
        </box>
        <text fg={toneColor(decision().tone)} wrapMode="word">{props.goal.status === 'running' ? decisionPulse() : decision().icon} {decision().text}</text>
        <Show when={activeDecision()}>
          {active => <text fg={C.textMuted} wrapMode="none" truncate>
            {phaseLabel(active().phase)}{active().round ? ` · 第 ${active().round} 轮` : ''} · {(active().tools || []).length} 次工具
          </text>}
        </Show>
        <For each={activeTools()}>{tool => <text fg={tool.status === 'failed' ? C.error : tool.status === 'running' ? C.info : C.textMuted} wrapMode="none" truncate>
          {'  '}{tool.status === 'running' ? '●' : tool.status === 'failed' ? '×' : '✓'} {tool.name}{tool.summary ? ` · ${tool.summary}` : ''}
        </text>}</For>
        <Show when={decisionHistory().length} fallback={<text fg={C.textMuted}>等待第一个模型决策</text>}>
          <For each={decisionHistory()}>{item =>
            <text fg={item.status === 'failed' ? C.error : C.textMuted} wrapMode="none" truncate>  {item.status === 'done' ? '✓' : '·'} {item.agent} · {item.text}</text>
          }</For>
        </Show>
      </box>

      <Show when={props.goal.status === 'running'}>
        <text fg={C.textMuted} wrapMode="word">需要介入时输入 /goal pause；当前 Agent 到达安全检查点后会暂停。</text>
      </Show>

      <box border borderStyle="rounded" borderColor={C.textMuted} paddingX={1} flexDirection="column" minWidth={0}>
        <text fg={C.secondary}>任务图</text>
        <Show when={!props.goal.tasks.length}>
          <text fg={C.textMuted} wrapMode="word">Task 契约尚未到达；Goal 可能正在恢复、生成任务，或事件仍在传输。</text>
        </Show>
        <For each={props.goal.tasks}>{(task, index) => {
          const active = () => task.id === props.goal.current_task_id && !terminal();
          const presentation = () => taskPresentation(task, active(), props.goal.tasks, props.goal);
          return <box flexDirection={compact() ? 'column' : 'row'} minWidth={0}>
            <box flexDirection="row" minWidth={0} flexGrow={1}>
              <text fg={toneColor(presentation().tone)} wrapMode="none">{presentation().icon} {String(index() + 1).padStart(2, '0')} </text>
              <text fg={active() ? C.text : C.textMuted} wrapMode="none" truncate flexGrow={1}>{task.subject}</text>
            </box>
            <text fg={toneColor(presentation().tone)} wrapMode="none">{compact() ? '    ' : ' · '}{presentation().text}</text>
          </box>;
        }}</For>
      </box>

      <Show when={current()}>
        {task => <>
          <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
            <box border borderStyle="rounded" borderColor={terminal() ? C.textMuted : C.warning} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={terminal() ? C.secondary : C.warning}>{terminal() ? '最后 Task' : '当前 Task'} · {task().subject}</text>
              <text fg={C.textMuted} wrapMode="word">Task cycles {props.goal.task_cycles || 0} · {verificationLabel(task().verification_state)}</text>
              <Show when={(task().acceptance_cases || []).length}>
                <text fg={C.secondary}>验收条件</text>
                <For each={task().acceptance_cases || []}>{(item, index) =>
                  <text fg={C.text} wrapMode="word">  {item.id || `AC${index() + 1}`} · 给定 {item.given || '-'} · 当 {item.when || '-'} · 则 {item.then || '-'}</text>
                }</For>
              </Show>
            </box>

            <box border borderStyle="rounded" borderColor={C.textMuted} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={C.secondary}>测试绑定</text>
              <text fg={task().verification_spec?.command ? C.info : C.warning} wrapMode="word">{task().verification_spec?.command || '测试尚未绑定'}</text>
              <Show when={(task().verification_spec?.selectors || []).length}>
                <For each={task().verification_spec?.selectors || []}>{selector => <text fg={C.textMuted} wrapMode="word">  ↳ {selector}</text>}</For>
              </Show>
              <text fg={C.textMuted}>来源 {sourceLabel(task().verification_spec?.source)} · 收集 {task().verification_spec?.collected_count || 0} 项</text>
              {(() => {
                const baseline = baselinePresentation(task().verification_spec?.baseline_result, task().verification_spec?.source);
                return <text fg={toneColor(baseline.tone)}>{baseline.icon} {baseline.text}</text>;
              })()}
            </box>
          </box>

          <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
            <box border borderStyle="rounded" borderColor={latest()?.exit_code === 0 ? C.success : (latest() ? C.error : C.textMuted)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={C.secondary}>最近一次机器证据</text>
              <Show when={latest()} fallback={<text fg={C.textMuted}>○ 尚未产生测试运行证据</text>}>
                {evidence => <>
                  <text fg={evidence().exit_code === 0 ? C.success : C.error}>
                    {evidence().exit_code === 0 ? '✓ 通过' : `× 退出码 ${evidence().exit_code ?? '-'}`} · {formatDuration(evidence().duration_ms)} · 收集 {evidence().collected_count || 0} 项
                  </text>
                  <Show when={evidence().code_snapshot}>
                    <text fg={C.textMuted} wrapMode="none" truncate>代码快照 {evidence().code_snapshot}</text>
                  </Show>
                  <For each={output()}>{line => <text fg={C.textMuted} wrapMode="word">  {line}</text>}</For>
                  <text fg={C.textMuted}>累计保存 {task().evidence_count || 0} 次验证结果</text>
                </>}
              </Show>
            </box>
            <box flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1} gap={1}>
              {gatePanel()}
              {regressionPanel()}
            </box>
          </box>
        </>}
      </Show>

      <Show when={!current()}>
        <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
          {gatePanel()}
          {regressionPanel()}
        </box>
      </Show>
    </box>
  </scrollbox>;
}
