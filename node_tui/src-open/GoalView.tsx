import {createRenderEffect, createSignal} from 'solid-js';
import {useRenderer} from '@opentui/solid';
import {GoalDetails} from './GoalDetails';
import {GoalSummary} from './GoalSummary';
import {goalDetailsExpanded, type GoalDraftSnapshot, type GoalSnapshot} from './goal-state';
import type {InteractionTrace} from './interaction-trace';
import {submitRenderFrame} from './interaction-trace';

export * from './goal-state.ts';

export type GoalTone = 'success' | 'warning' | 'error' | 'info' | 'muted';
export type GoalPresentation = {tone: GoalTone; icon: string; text: string};
export type GoalDecision = {id?: string; runId?: string; phase?: string; agent?: string; model?: string; text?: string; status?: string; at?: number; startedAt?: number; elapsed?: number; round?: number; tools?: unknown[]};

export function goalStatusPresentation(status: string): GoalPresentation {
  if (status === 'done') return {tone: 'success', icon: '✓', text: '已交付'};
  if (status === 'failed') return {tone: 'error', icon: '×', text: '失败'};
  if (status === 'cancelled') return {tone: 'muted', icon: '■', text: '已取消'};
  if (status === 'paused' || status === 'pausing') return {tone: 'warning', icon: 'Ⅱ', text: status === 'pausing' ? '正在暂停' : '已暂停'};
  return {tone: 'info', icon: '●', text: '执行中'};
}

export function goalSupervisorPresentation(supervision?: any): GoalPresentation {
  if (!supervision) return {tone: 'muted', icon: '○', text: '尚未启动'};
  if (supervision.status === 'unavailable') return {tone: 'error', icon: '×', text: '暂时不可用，确定性规则仍在运行'};
  if (supervision.status === 'attention') return {tone: 'warning', icon: '!', text: '发现需要处理的边界'};
  if (supervision.status === 'observing') return {tone: 'info', icon: '●', text: '并行观察中'};
  return {tone: 'muted', icon: '○', text: supervision.status || '等待事件'};
}

export function baselinePresentation(result?: string, source?: string): GoalPresentation {
  if (source === 'needs_generation') return {tone: 'warning', icon: '○', text: '失败基线：等待生成测试'};
  if (result === 'failing') return {tone: 'success', icon: '✓', text: '失败基线：已确认（实现前测试失败）'};
  if (source && source !== 'generated') return {tone: 'muted', icon: '·', text: '失败基线：不适用（复用已有测试）'};
  return {tone: 'warning', icon: '○', text: '失败基线：尚未确认'};
}

export function gatePresentation(goal: any, task?: any): GoalPresentation {
  if (goal?.status === 'done') return {tone: 'success', icon: '✓', text: '全部 Task 证据与最终回归均已通过，可以交付'};
  if (goal?.status === 'failed') return {tone: 'error', icon: '×', text: '机器门禁未通过，Goal 已停止'};
  if (goal?.status === 'paused' && goal?.stop_reason === 'permission_wait') return {tone: 'warning', icon: '!', text: '等待工具权限（approval）后才能继续当前 Task'};
  if (goal?.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: 'Goal 已暂停，可以恢复后继续验证'};
  if (goal?.phase === 'verify') return {tone: 'warning', icon: '●', text: '正在等待绑定测试的机器结果'};
  return {tone: 'info', icon: '●', text: '正在执行'};
}

export function goalRegressionPresentation(goal: any): GoalPresentation {
  const result = goal?.final_verification;
  if (result?.status === 'failed' || result?.exit_code > 0) return {tone: 'error', icon: '×', text: `失败 · exit ${result.exit_code ?? 1}`};
  if (result?.status === 'passed' || (goal?.status === 'done' && goal?.phase === 'done')) {
    if (result?.exit_code === 0 && result?.duration_ms != null) return {tone: 'success', icon: '✓', text: `通过 · exit 0 · ${result.duration_ms} ms`};
    return {tone: 'success', icon: '✓', text: '通过'};
  }
  return {tone: 'warning', icon: '○', text: '尚未完成最终回归'};
}

export function goalDecisionPresentation(goal: any, decisions: GoalDecision[]): any {
  const history = decisions.slice(-4);
  if (goal?.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', title: '模型决策流', owner: '已暂停', text: goal.last_error || '已保存当前进度，等待恢复', history};
  const active = [...decisions].reverse().find(d => d.status === 'active');
  if (active) return {tone: 'info', icon: '●', title: '模型决策流', owner: active.model ? `${active.agent} · ${active.model}` : (active.agent || ''), text: active.text || '', history};
  return {tone: 'warning', icon: '●', title: '模型决策流', owner: goal?.phase || '', text: '正在准备下一步', history};
}

export function goalNextActionPresentation(goal: any): any {
  if (goal?.status === 'done') return {tone: 'success', icon: '✓', text: '已完成', command: '开始新的 /goal', detail: '全部任务已完成'};
  if (goal?.status === 'paused' && goal?.stop_reason === 'user_approval_required') return {tone: 'warning', icon: '!', text: '等待批准', command: '/goal run', detail: '批准后继续'};
  if (goal?.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: '已暂停', command: '/goal resume', detail: '恢复当前 Goal'};
  return {tone: 'info', icon: '●', text: '执行中', command: '/goal status', detail: '查看进度'};
}

export function goalDraftStagePresentation(draft: any): GoalPresentation { return {tone: draft?.stage === 'ready' ? 'success' : 'info', icon: draft?.stage === 'ready' ? '✓' : '●', text: draft?.stage || '准备中'}; }
export function goalDraftNextActionPresentation(draft: any): any { return {tone: 'info', icon: '●', text: '继续', command: draft?.question ? '/goal answer' : '/goal approve', detail: '继续 Goal 草稿'}; }
export function goalDraftStageRail(draft: any): any[] { const stages = ['intake','discovering','planning','ready']; const idx = stages.indexOf(draft?.stage); return stages.map((id,i)=>({id,label:id,status:i < idx ? 'done' : i===idx ? 'active' : 'pending'})); }
export function goalDraftHeartbeatPresentation(draft: any, now = Date.now()): any { const stale = draft?.last_heartbeat && now - draft.last_heartbeat > 30_000; return {tone: stale ? 'error' : 'success', icon: stale ? '×' : '✓', text: stale ? '可能停滞' : '运行正常', elapsed: '', deadline: ''}; }
export function goalDraftAgentRows(draft: any): any[] { return (draft?.agents || []).map((a: any) => ({label:a.role || a.agent_type || 'Agent', status:a.status || 'running', activity:a.activity || a.last_text || '', meta:a.model || ''})); }
export function goalExecutionStageRail(goal: any): any[] { const stages = [{id:'act',label:'实现'},{id:'review',label:'评审'},{id:'regression',label:'回归'}]; const i = goal?.phase === 'evaluate' ? 1 : goal?.phase === 'done' ? 2 : 0; return stages.map((s,n)=>({...s,status:n<i?'done':n===i?'active':'pending'})); }

type GoalLike = GoalSnapshot | GoalDraftSnapshot;

export type GoalViewProps = {
  goal?: GoalLike | null;
  draft?: GoalDraftSnapshot | null;
  snapshot?: GoalLike | null;
  decisions?: readonly GoalDecision[];
  onExpandDetails?: () => void;
  onToggleDetails?: (expanded: boolean) => void;
  interactionTrace?: InteractionTrace;
  [key: string]: unknown;
};

function fallbackGoal(): GoalDraftSnapshot {
  return {
    id: 'goal', target: '暂无 Goal', status: 'ready', stage: 'intake',
    intake_assumptions: [], clarifications: [], question_index: 0, question_count: 0,
    task_count: 0, tasks: [], agents: [], discovery_jobs: [],
  };
}

export function GoalView(props: GoalViewProps) {
  // 展开状态只属于本次 GoalView 会话，不从快照或配置中读取，也不写回持久状态。
  const [detailsExpanded, setDetailsExpanded] = createSignal(false);
  const renderer = useRenderer();
  const selectedGoal = (): GoalLike => props.goal || props.snapshot || props.draft || fallbackGoal();
  let initialRender = true;
  createRenderEffect(() => {
    detailsExpanded();
    if (initialRender) {
      initialRender = false;
      return;
    }
    // 仅在展开 signal 已触发 Solid 响应式更新后提交，确保详情分支可被离屏 renderer 观察。
    submitRenderFrame(renderer, props.interactionTrace);
  });
  const toggleDetails = () => {
    const before = detailsExpanded();
    const next = goalDetailsExpanded(before, 'toggle');
    props.interactionTrace?.record({event: 'state_before', target: 'GOAL_DETAILS_TOGGLE', state_before: {expanded: before}});
    setDetailsExpanded(next);
    props.onToggleDetails?.(next);
    props.interactionTrace?.record({event: 'state_after', target: 'GOAL_DETAILS_TOGGLE', state_after: {expanded: next}});
    // 帧提交由 signal 的同步响应式 effect 负责，避免早于详情分支更新。
  };
  const toggleFromSummary = () => {
    toggleDetails();
    props.onExpandDetails?.();
  };

  return <box flexDirection="column" width="100%" height="100%"><GoalSummary goal={selectedGoal()} decisions={props.decisions} onExpandDetails={toggleFromSummary} /><GoalDetails goal={selectedGoal()} expanded={detailsExpanded} onToggle={toggleDetails} interactionTrace={props.interactionTrace} /></box>;
}

export function GoalDraftView(props: {draft: GoalDraftSnapshot; now?: number; width: number; height: number}) {
  const draft = props.draft;
  return <box flexDirection="column" width="100%" height="100%">
    <GoalSummary goal={draft} decisions={[]} onExpandDetails={() => {}} />
    <box flexDirection="column">
      <text content={`草稿阶段：${draft.stage || '准备中'} · ${draft.task_count || draft.tasks?.length || 0} 个任务`} />
      <Show when={draft.question}><text content={`待回答：${draft.question}`} /></Show>
    </box>
  </box>;
}

export default GoalView;
