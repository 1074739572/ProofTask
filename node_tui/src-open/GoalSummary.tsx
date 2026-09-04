import {Show} from 'solid-js';
import type {GoalDraftSnapshot, GoalDraftTaskSummary, GoalSnapshot, GoalTaskSnapshot} from './goal-state.ts';
import {C} from './theme.ts';
import {clipTerminalText, layoutMode, type LayoutMode} from './layout.ts';

type GoalLike = GoalSnapshot | GoalDraftSnapshot;
type TaskLike = GoalTaskSnapshot | GoalDraftTaskSummary;
type GoalSource = GoalLike | (() => GoalLike | null | undefined);
type DecisionsSource = readonly DecisionLike[] | (() => readonly DecisionLike[] | undefined);

type DecisionLike = {
  agent?: string;
  text?: string;
  status?: string;
  phase?: string;
};

export type GoalSummaryProps = {
  goal: GoalSource;
  decisions?: DecisionsSource;
  onExpandDetails?: () => void;
  /** Terminal width used to keep the first screen readable on narrow TTYs. */
  width?: number | (() => number);
  /** Available main-view height; short terminals need a stricter summary. */
  height?: number | (() => number);
  /** Animation frame counter (~80ms). Drives the progress-bar head pulse;
   * optional so off-screen previews stay static. */
  tick?: () => number;
};

function safeText(value: unknown, fallback = '暂无'): string {
  const text = value == null ? fallback : String(value).trim();
  return text || fallback;
}

function readSource<T>(source: T | (() => T) | undefined): T | undefined {
  return typeof source === 'function' ? (source as () => T)() : source;
}

function fallbackGoal(): GoalDraftSnapshot {
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

function clip(value: unknown, width: number): string {
  return clipTerminalText(safeText(value), width);
}

function isSnapshot(goal: GoalLike): goal is GoalSnapshot {
  return 'phase' in goal;
}

function statusLabel(status: string): string {
  switch (status) {
    case 'running': return '执行中';
    case 'paused': return '已暂停';
    case 'failed': return '失败';
    case 'permission_wait': return '已暂停（等待权限批准）';
    case 'pausing': return '正在暂停';
    case 'cancelling': return '正在取消';
    case 'ready': return '待开始';
    case 'approved': return '已批准';
    case 'completed': return '已完成';
    default: return safeText(status, '未知状态');
  }
}

function phaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    intake: '需求', catalog: '测试准备', prepare_tests: '测试准备', planning: '规划',
    preflight: '预检', discovering: '发现', act: '实现', working: '实现',
    verify: '验证', verification: '验证', completed: '完成', paused: '暂停', failed: '失败',
  };
  return labels[phase] || safeText(phase, '准备');
}

function statusOf(goal: GoalLike): string {
  return safeText(goal.status, 'unknown');
}

function phaseOf(goal: GoalLike): string {
  return isSnapshot(goal) ? safeText(goal.phase, 'intake') : safeText(goal.stage, 'intake');
}

function taskFor(goal: GoalLike): TaskLike | undefined {
  if (isSnapshot(goal)) {
    if (goal.current_task_id) return goal.tasks.find(task => task.id === goal.current_task_id);
    return goal.tasks.find(task => /^(?:running|in_progress|active)$/i.test(task.status));
  }
  return goal.tasks[0];
}

function currentAgentFor(goal: GoalLike, decisions: readonly DecisionLike[] | undefined): string {
  const decision = decisions?.find(item => item.status === 'active') || decisions?.[0];
  if (decision?.agent) return safeText(decision.agent);
  if (isSnapshot(goal)) return '暂无';
  const agent = goal.agents.find(item => item.status === 'running') || goal.agents[0];
  return agent ? safeText(agent.role || agent.agent_type || agent.id) : '暂无';
}

function errorFor(goal: GoalLike): string {
  return safeText(goal.last_error || (isSnapshot(goal) ? goal.final_verification?.error : undefined));
}

function stopReasonOf(goal: GoalLike): string {
  return isSnapshot(goal) ? safeText(goal.stop_reason, '无') : '无';
}

function permissionIsWaiting(goal: GoalLike): boolean {
  return statusOf(goal) === 'permission_wait' || stopReasonOf(goal) === 'permission_wait';
}

function nextAction(goal: GoalLike): string {
  const status = statusOf(goal);
  if (permissionIsWaiting(goal)) return '/goal resume（批准后继续）';
  if (status === 'paused') return '/goal resume';
  if (status === 'failed') return '/goal status（查看失败详情）';
  if (status === 'ready') return '/goal start';
  if (status === 'completed' || status === 'consumed') return '/goal status（查看结果）';
  return '/goal pause';
}

function statusColor(status: string): string {
  if (status === 'done' || status === 'completed') return C.success;
  if (status === 'failed') return C.error;
  if (status === 'paused' || status === 'pausing' || status === 'permission_wait') return C.warning;
  return C.primary;
}

function statusIcon(status: string): string {
  if (status === 'done' || status === 'completed') return '✓';
  if (status === 'failed') return '×';
  if (status === 'paused' || status === 'pausing' || status === 'permission_wait') return 'Ⅱ';
  return '●';
}

function progressFor(goal: GoalLike): {done: number; total: number; percent: number} {
  const tasks = goal.tasks || [];
  const done = tasks.filter(task => 'status' in task && /^(done|completed|passing)$/i.test(task.status)).length;
  const total = tasks.length;
  return {done, total, percent: total > 0 ? Math.round(done / total * 100) : 0};
}

function progressFilled(percent: number, width: number): number {
  return Math.max(0, Math.min(width, Math.round(percent / 100 * width)));
}

const TRACK: readonly [string, string][] = [
  ['intake', '需求'], ['prepare_tests', '测试准备'], ['planning', '规划'],
  ['discovering', '发现'], ['act', '实现'], ['verify', '验证'], ['completed', '完成'],
];

function phaseIndex(phase: string): number {
  const index = TRACK.findIndex(([key]) => key === phase);
  if (index >= 0) return index;
  if (phase === 'catalog' || phase === 'preflight') return 1;
  if (phase === 'working') return 4;
  if (phase === 'verification' || phase === 'failed' || phase === 'paused') return 5;
  return 0;
}

export function GoalSummary(props: GoalSummaryProps) {
  const goal = () => readSource(props.goal) || fallbackGoal();
  const decisions = () => readSource(props.decisions) || [];
  const status = () => statusOf(goal());
  const phase = () => phaseOf(goal());
  const currentDecision = () => decisions().find(item => item.status === 'active') || decisions()[0];
  const currentTask = () => taskFor(goal());
  const activePhase = () => phaseIndex(phase());
  const permissionWaiting = () => permissionIsWaiting(goal());
  const displayStatus = () => permissionWaiting() ? 'permission_wait' : status();
  const showRecovery = () => status() === 'paused' || status() === 'failed';
  const width = () => Math.max(1, Number(readSource(props.width)) || 120);
  const height = () => Math.max(1, Number(readSource(props.height)) || 28);
  const mode = (): LayoutMode => layoutMode(width(), height());
  const narrow = () => mode() !== 'wide';
  const short = () => mode() === 'short';
  const track = () => TRACK.map(([key, label], index) => `${index <= activePhase() ? '●' : '○'}${label}`).join(' → ');
  const taskLabel = () => {
    const task = currentTask();
    const tasks = goal().tasks;
    if (task && tasks.length <= 1) return clip('subject' in task ? task.subject : task.name, 54);
    if (isSnapshot(goal()) && tasks.length > 0) {
      const index = task ? tasks.findIndex((item: any) => item.id === (task as any).id) + 1 : 0;
      return index > 0 ? `第 ${index} 个任务（共 ${tasks.length} 个）` : `共 ${tasks.length} 个任务，当前任务待分配`;
    }
    if (!isSnapshot(goal()) && tasks.length > 0) return `共 ${tasks.length} 个阶段任务`;
    return '暂无当前任务';
  };
  const progress = () => progressFor(goal());
  const metricWidth = () => short() ? 34 : narrow() ? 52 : 68;
  // Progress light-band: the bar renders in three segments — done (success),
  // a pulsing head cell at the frontier (primary), and the remainder (muted).
  // The head breathes on the animation clock while the goal is incomplete.
  const barWidth = () => short() ? 12 : narrow() ? 22 : 34;
  const barFilled = () => progressFilled(progress().percent, barWidth());
  const barHasHead = () => progress().total > 0 && barFilled() < barWidth();
  const barHead = () => {
    if (!barHasHead()) return '';
    const t = props.tick?.() ?? 0;
    return t % 6 < 3 ? '╸' : '─';
  };
  const barRest = () => '─'.repeat(Math.max(0, barWidth() - barFilled() - (barHasHead() ? 1 : 0)));

  const statusText = () => `${statusIcon(displayStatus())} ${statusLabel(displayStatus())}`;
  const progressText = () => progress().total ? `${progress().done}/${progress().total} Tasks · ${progress().percent}%` : '任务图准备中';
  const decisionText = () => clip(currentDecision()?.text, short() ? 52 : narrow() ? 78 : 120);
  // The compact shell renders its own one-line details affordance directly
  // below the summary.  Keep the extra hint only where it adds information:
  // short terminals need the explicit key, while wide terminals benefit from
  // the longer mouse/keyboard wording beside the inspector.
  const detailsHint = () => short() ? '[d] 查看任务 / Agent / 证据' : mode() === 'wide' ? '详情 · Enter / Space 或 d 查看任务、Agent 与机器证据' : '';

  // A compact execution summary replaces the former three-card row.  The
  // fields are ordered by actionability so short terminals lose IDs and
  // decoration before they lose the current action or recovery command.
  return <box flexDirection="column" flexGrow={0} flexShrink={0} minWidth={0} paddingX={1} paddingTop={short() ? 0 : 1}>
    <box border={!short()} borderStyle="rounded" borderColor={statusColor(displayStatus())} flexDirection="column" flexShrink={0} paddingX={short() ? 0 : 1} minWidth={0}>
      <box flexDirection={mode() === 'wide' ? 'row' : 'column'} justifyContent="space-between" minWidth={0} flexShrink={1}>
        <text fg={C.primary} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{short() ? 'GOAL' : `GOAL · ${clip(goal().target, mode() === 'wide' ? 74 : 54)}`}</text>
        <text fg={statusColor(displayStatus())} wrapMode="none" truncate flexShrink={0}>{statusText()}</text>
      </box>
      <Show when={!short()} fallback={<box />}>
        <text fg={C.textMuted} wrapMode="none" truncate flexShrink={1}>{mode() === 'wide' ? `ID ${clip(goal().id, 52)} · ` : ''}阶段 {phaseLabel(phase())}</text>
      </Show>
      <Show when={short()} fallback={<box />}><text fg={C.text} wrapMode="none" truncate>{clip(goal().target, 48)}</text></Show>
      <box flexDirection="row" minWidth={0} marginTop={short() ? 0 : 1}>
        <text fg={C.success} wrapMode="none" flexShrink={0}>{'━'.repeat(barFilled())}</text>
        <text fg={C.primary} wrapMode="none" flexShrink={0}>{barHead()}</text>
        <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{barRest()}</text>
        <text fg={C.text} wrapMode="none" truncate flexGrow={1} flexShrink={1}>  {progressText()}</text>
      </box>
      <Show when={!short()} fallback={<box />}><text fg={C.secondary} wrapMode="none" truncate flexShrink={1}>执行链路  {track()}</text></Show>
    </box>

    <box flexDirection="column" minWidth={0} flexShrink={0} marginTop={short() ? 0 : 1}>
      <text fg={C.info} wrapMode="none" truncate>Task  {clip(taskLabel(), metricWidth())}</text>
      <text fg={C.secondary} wrapMode="none" truncate>Agent {clip(currentAgentFor(goal(), decisions()), metricWidth())}</text>
      <Show when={!short() || currentDecision()?.text} fallback={<box />}>
        <text fg={C.textMuted} wrapMode="word" truncate>动作  {decisionText() || (status() === 'running' ? '正在准备下一步' : '暂无活动')}</text>
      </Show>
      <text fg={permissionWaiting() ? C.warning : C.success} wrapMode="none" truncate>下一步 {clip(nextAction(goal()), metricWidth())}</text>
    </box>

    <Show when={currentDecision()?.text && !short()} fallback={<box />}>
      {/* OpenTUI treats a supplied borderStyle/borderColor as an implicit
       * border (even when `border={false}`).  Keep those props conditional so
       * compact layouts do not gain an accidental four-row card whose bottom
       * edge collides with the details affordance. */}
      <box border={mode() === 'wide'} borderStyle={mode() === 'wide' ? 'rounded' : undefined} borderColor={mode() === 'wide' ? C.textMuted : undefined} flexDirection="column" minWidth={0} marginTop={1} paddingX={mode() === 'wide' ? 1 : 0}>
        <text fg={C.secondary} wrapMode="none" truncate>LIVE ACTION · Agent 正在做什么</text>
        <text fg={C.text} wrapMode="word" truncate>{decisionText()}</text>
      </box>
    </Show>
    <Show when={!short() && (mode() === 'wide' || permissionWaiting() || showRecovery())} fallback={<box />}>
      <text fg={permissionWaiting() ? C.warning : C.textMuted} wrapMode="none" truncate>权限：{permissionWaiting() ? '等待工具权限批准，可批准后恢复' : '无需批准'}</text>
      <Show when={showRecovery()} fallback={<box />}><text fg={C.warning} wrapMode="word" truncate>状态说明：{permissionWaiting() ? '等待批准后可恢复执行' : clip(errorFor(goal()), 80)}</text></Show>
    </Show>
    <Show when={detailsHint()} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate selectable={false}>{detailsHint()}</text></Show>
  </box>;
}

export default GoalSummary;
