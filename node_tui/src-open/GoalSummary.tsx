import type {GoalDraftSnapshot, GoalDraftTaskSummary, GoalSnapshot, GoalTaskSnapshot} from './goal-state.ts';

type GoalLike = GoalSnapshot | GoalDraftSnapshot;
type TaskLike = GoalTaskSnapshot | GoalDraftTaskSummary;

type DecisionLike = {
  agent?: string;
  text?: string;
  status?: string;
  phase?: string;
};

export type GoalSummaryProps = {
  goal: GoalLike;
  decisions?: readonly DecisionLike[];
  onExpandDetails?: () => void;
};

function safeText(value: unknown, fallback = '暂无'): string {
  const text = value == null ? fallback : String(value).trim();
  return text || fallback;
}

function clip(value: unknown, width: number): string {
  const text = safeText(value);
  return text.length > width ? `${text.slice(0, Math.max(1, width - 1))}…` : text;
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
  const status = () => statusOf(props.goal);
  const phase = () => phaseOf(props.goal);
  const currentDecision = () => props.decisions?.find(item => item.status === 'active') || props.decisions?.[0];
  const currentTask = () => taskFor(props.goal);
  const activePhase = () => phaseIndex(phase());
  const permissionWaiting = () => permissionIsWaiting(props.goal);
  const displayStatus = () => permissionWaiting() ? 'permission_wait' : status();
  const showRecovery = () => status() === 'paused' || status() === 'failed';
  const track = () => TRACK.map(([key, label], index) => `${index <= activePhase() ? '●' : '○'}${label}`).join(' → ');
  const taskLabel = () => {
    const task = currentTask();
    const tasks = props.goal.tasks;
    if (task && tasks.length <= 1) return clip('subject' in task ? task.subject : task.name, 54);
    if (isSnapshot(props.goal) && tasks.length > 0) {
      const index = task ? tasks.findIndex(item => item.id === task.id) + 1 : 0;
      return index > 0 ? `第 ${index} 个任务（共 ${tasks.length} 个）` : `共 ${tasks.length} 个任务，当前任务待分配`;
    }
    if (!isSnapshot(props.goal) && tasks.length > 0) return `共 ${tasks.length} 个阶段任务`;
    return '暂无当前任务';
  };

  return <box flexDirection="column" width="100%">
    <text content={`Goal · ${clip(props.goal.target, 68)}`} />
    <text content={`状态：${statusLabel(displayStatus())}  ·  当前阶段：${phaseLabel(phase())}`} />
    <text content={`执行链路：${track()}`} />
    <text content={`当前 Agent：${clip(currentAgentFor(props.goal, props.decisions), 56)}`} />
    <text content={`当前 Task：${taskLabel()}`} />
    <text content={`Agent 动作：${clip(currentDecision()?.text, 54)}`} />
    <text content={`权限：${permissionWaiting() ? '等待工具权限批准，可批准后恢复' : '无需批准'}`} />
    <text content={`状态说明：${permissionWaiting() ? '等待批准后可恢复执行' : showRecovery() ? clip(errorFor(props.goal), 60) : '正在按阶段轨道执行'}`} />
    <text content={`下一步：${nextAction(props.goal)}`} />
    <text content="详情默认折叠，按需展开查看完整信息" />
  </box>;
}

export default GoalSummary;
