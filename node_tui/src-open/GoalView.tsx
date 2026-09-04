import {createMemo, createRenderEffect, createSignal, For, Show} from 'solid-js';
import {useKeyboard, useRenderer} from '@opentui/solid';
import {GoalDetails} from './GoalDetails.tsx';
import {GoalSummary} from './GoalSummary.tsx';
import {goalDetailsExpanded, type GoalDraftSnapshot, type GoalSnapshot} from './goal-state.ts';
import type {InteractionTrace} from './interaction-trace.ts';
import {submitRenderFrame} from './interaction-trace.ts';
import {clipTerminalText, layoutMode, type LayoutMode} from './layout.ts';
import {C} from './theme.ts';

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
export function goalDraftHeartbeatPresentation(draft: any, now = Date.now()): any { const heartbeat = Number(draft?.last_heartbeat || 0); const normalized = heartbeat > 0 && heartbeat < 1e11 ? heartbeat * 1000 : heartbeat; const stale = normalized > 0 && now - normalized > 30_000; return {tone: stale ? 'error' : 'success', icon: stale ? '×' : '✓', text: stale ? '可能停滞' : '运行正常', elapsed: '', deadline: ''}; }
export function goalDraftAgentRows(draft: any): any[] { const labels: Record<string,string> = {architecture:'架构路径', implementation:'实现路径', tests:'测试路径', history:'历史路径'}; return (draft?.agents || []).map((a: any) => { const latest = a.rounds?.[a.rounds.length - 1]; const job = (draft?.discovery_jobs || []).find((j: any) => j.role === a.role || j.role === a.agent_type?.replace(/^goal_discovery_/, '')); const meta = [latest?.round ? `第 ${latest.round} 轮` : '', job?.read_path_count ? `${job.read_path_count} 个文件` : '', a.model || ''].filter(Boolean).join(' · '); return {label:labels[a.role] || labels[a.agent_type] || a.role || a.agent_type || 'Agent', status:a.status || 'running', activity:a.activity || a.last_text || latest?.text || '', meta}; }); }
export function goalExecutionStageRail(goal: any): any[] { const stages = [{id:'act',label:'实现'},{id:'review',label:'评审'},{id:'regression',label:'回归'}]; const i = goal?.phase === 'evaluate' ? 1 : goal?.phase === 'done' ? 2 : 0; return stages.map((s,n)=>({...s,status:n<i?'done':n===i?'active':'pending'})); }

type GoalLike = GoalSnapshot | GoalDraftSnapshot;
type GoalSource = GoalLike | (() => GoalLike | null | undefined);
type DraftSource = GoalDraftSnapshot | (() => GoalDraftSnapshot | null | undefined);
type DecisionsSource = readonly GoalDecision[] | (() => readonly GoalDecision[] | undefined);

function readSource<T>(source: T | (() => T) | undefined): T | undefined {
  return typeof source === 'function' ? (source as () => T)() : source;
}

export type GoalViewProps = {
  goal?: GoalSource;
  draft?: DraftSource;
  snapshot?: GoalSource;
  decisions?: DecisionsSource;
  onExpandDetails?: () => void;
  onToggleDetails?: (expanded: boolean) => void;
  interactionTrace?: InteractionTrace;
  width?: number | (() => number);
  height?: number | (() => number);
  composerEmpty?: () => boolean;
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
  const viewWidth = () => Math.max(1, Number(readSource(props.width)) || 120);
  const viewHeight = () => Math.max(1, Number(readSource(props.height)) || 28);
  // Expansion is a session-local opt-in on every terminal size.  This keeps
  // the existing Goal contract (details collapsed by default) while the wide
  // shell still reserves a stable inspector column for the affordance.
  const [detailsExpanded, setDetailsExpanded] = createSignal(false);
  const renderer = useRenderer();
  // Resolve the snapshot through a memo so function-valued props (the live
  // Goal signal supplied by App/debug harnesses) remain a tracked dependency
  // even when the child view itself stays mounted across lifecycle updates.
  const selectedGoal = createMemo<GoalLike>(() => readSource(props.goal) || readSource(props.snapshot) || readSource(props.draft) || fallbackGoal());
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

  useKeyboard((event: any) => {
    if (props.composerEmpty && !props.composerEmpty()) return;
    if (event?.ctrl || event?.meta || event?.alt) return;
    const key = String(event?.name || event?.key || '').toLowerCase();
    if (key === 'd' || key === 'enter' || key === 'return') {
      toggleDetails();
      event.preventDefault?.();
    }
  });

  const summary = <GoalSummary
    goal={selectedGoal}
    decisions={() => readSource(props.decisions) || []}
    width={viewWidth}
    height={viewHeight}
    tick={typeof props.tick === 'function' ? props.tick as () => number : undefined}
    onExpandDetails={toggleFromSummary}
  />;
  const details = <GoalDetails
    goal={selectedGoal}
    expanded={detailsExpanded}
    onToggle={toggleDetails}
    interactionTrace={props.interactionTrace}
    decisions={() => readSource(props.decisions) || []}
    width={viewWidth}
    height={viewHeight}
  />;
  return <box flexDirection={layoutMode(viewWidth(), viewHeight()) === 'wide' ? 'row' : 'column'} flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} height="100%">
    <box flexDirection="column" width={layoutMode(viewWidth(), viewHeight()) === 'wide' ? '64%' : '100%'} flexGrow={layoutMode(viewWidth(), viewHeight()) === 'wide' ? 0 : 1} flexShrink={1} minHeight={0} minWidth={0}>{summary}</box>
    <box flexDirection="column" width={layoutMode(viewWidth(), viewHeight()) === 'wide' ? '36%' : '100%'} flexGrow={layoutMode(viewWidth(), viewHeight()) === 'wide' ? 1 : 0} flexShrink={1} minHeight={0} minWidth={0}>{details}</box>
  </box>;
}

export function GoalDraftView(props: {draft: DraftSource; now?: number | (() => number); width: number | (() => number); height: number | (() => number)}) {
  // The parent keeps this branch mounted across draft updates (Switch/Match),
  // so the snapshot must be read reactively instead of captured once.
  const draft = () => readSource(props.draft) || fallbackGoal();
  const width = () => Math.max(1, Number(readSource(props.width)) || 120);
  const height = () => Math.max(1, Number(readSource(props.height)) || 28);
  const mode = (): LayoutMode => layoutMode(width(), height());
  const short = () => mode() === 'short';
  const stageLabels: Record<string, string> = {intake: '需求', discovering: '发现', planning: '规划', ready: '就绪'};
  const statusColor = (status: string): string => {
    if (/^(done|completed|ready|passed)$/i.test(status)) return C.success;
    if (/^(failed|error|stalled)$/i.test(status)) return C.error;
    if (/^(running|active|discovering)$/i.test(status)) return C.info;
    return C.textMuted;
  };
  const statusLabel = (status: string): string => {
    const labels: Record<string, string> = {running: '运行中', discovering: '探索中', done: '完成', failed: '失败', queued: '排队', pending: '等待', ready: '就绪'};
    return labels[status] || status || '等待';
  };
  const clip = (value: unknown, max: number) => {
    const text = String(value ?? '').replace(/\s+/g, ' ').trim();
    return clipTerminalText(text, max);
  };
  const stageRail = () => goalDraftStageRail(draft()).map(item => `${item.status === 'done' ? '●' : item.status === 'active' ? '◉' : '○'}${stageLabels[item.id] || item.id}`).join(' → ');
  const now = () => Number(readSource(props.now)) || Date.now();
  const heartbeat = () => goalDraftHeartbeatPresentation(draft(), now());
  const agents = () => goalDraftAgentRows(draft());
  const discoveryDone = () => Number(draft().discovery_completed ?? 0);
  const discoveryTotal = () => Number(draft().discovery_total ?? draft().discovery_jobs?.length ?? 0);
  const discoveryProgress = () => discoveryTotal() > 0 ? `${discoveryDone()}/${discoveryTotal()} 个探索任务` : `${agents().length} 个 Agent`;
  const next = () => goalDraftNextActionPresentation(draft());
  const target = () => String(draft().target || '暂无 Goal 草稿').trim();
  const message = () => String(draft().message || draft().intake_summary || '').trim();
  const question = () => String(draft().question || '').trim();

  return <box flexDirection="column" flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} height="100%">
    <box flexDirection="column" flexShrink={0} minWidth={0} paddingX={1} paddingTop={short() ? 0 : 1}>
      <box border={!short()} borderStyle="rounded" borderColor={statusColor(draft().status)} flexDirection="column" minWidth={0} paddingX={short() ? 0 : 1}>
        <box flexDirection={mode() === 'wide' ? 'row' : 'column'} justifyContent="space-between" minWidth={0}>
          <text fg={C.primary} wrapMode="none" truncate flexGrow={1}>DRAFT · {clip(target(), mode() === 'wide' ? 72 : 48)}</text>
          <text fg={statusColor(draft().status)} wrapMode="none" truncate>{statusLabel(draft().status)} · heartbeat {heartbeat().icon} {heartbeat().text}</text>
        </box>
        <Show when={!short()} fallback={<box />}><text fg={C.text} wrapMode="none" truncate>{clip(target(), 100)}</text></Show>
        <text fg={C.secondary} wrapMode="none" truncate>{stageRail()}</text>
        <text fg={C.textMuted} wrapMode="none" truncate>{discoveryProgress()}{draft().verification ? ` · 验证 ${clip(draft().verification, 36)}` : ''}</text>
      </box>
      <Show when={message() && !short()} fallback={<box />}><text fg={C.textMuted} wrapMode="word" truncate marginTop={1}>{message()}</text></Show>
      <Show when={question()} fallback={<box />}>
        <box border borderStyle="rounded" borderColor={C.warning} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
          <text fg={C.warning} wrapMode="none" truncate>! 需要回答</text>
          <text fg={C.text} wrapMode="word">{question()}</text>
          <text fg={C.warning} wrapMode="none" truncate>Enter 回答 · /goal pause 暂停</text>
        </box>
      </Show>
    </box>
    <scrollbox flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
      <box flexDirection="column" minWidth={0} paddingX={2} paddingBottom={1}>
        <text fg={C.secondary} wrapMode="none" truncate>AGENT LIVE · {agents().length} 个</text>
        <Show when={agents().length > 0} fallback={<text fg={C.textMuted}>尚未收到 Agent 现场事件</text>}>
          <For each={agents()}>{agent => <box flexDirection="column" minWidth={0} marginTop={short() ? 0 : 1}>
            <box flexDirection="row" minWidth={0}>
              <text fg={statusColor(agent.status)} wrapMode="none" flexShrink={0}>{/^(done|completed)$/i.test(agent.status) ? '✓' : /failed|error/i.test(agent.status) ? '×' : agent.status === 'queued' ? '○' : '●'} </text>
              <text fg={C.text} wrapMode="none" truncate flexGrow={1}>{agent.label}</text>
              <text fg={C.textMuted} wrapMode="none" truncate>{statusLabel(agent.status)}</text>
            </box>
            <Show when={!short() && agent.activity} fallback={<box />}><text fg={C.textMuted} wrapMode="word" truncate>  {clip(agent.activity, 100)}</text></Show>
            <Show when={!short() && agent.meta} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate>  {agent.meta}</text></Show>
          </box>}</For>
        </Show>
        <Show when={!short() && draft().discovery_jobs?.length > 0} fallback={<box />}>
          <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>DISCOVERY JOBS</text>
          <For each={draft().discovery_jobs}>{job => <text fg={statusColor(job.status)} wrapMode="none" truncate>{job.status === 'done' ? '✓' : job.status === 'running' ? '●' : job.status === 'failed' ? '×' : '○'} {job.role} · {statusLabel(job.status)} · {job.read_path_count || 0} 个文件</text>}</For>
        </Show>
        <Show when={!short() && draft().intake_assumptions?.length > 0} fallback={<box />}>
          <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>ASSUMPTIONS</text>
          <For each={draft().intake_assumptions}>{item => <text fg={C.textMuted} wrapMode="word">· {item}</text>}</For>
        </Show>
        <box flexDirection="column" minWidth={0} marginTop={1}>
          <text fg={C.success} wrapMode="none" truncate>下一步 · {next().command}</text>
          <text fg={C.textMuted} wrapMode="word" truncate>{next().detail}</text>
          <Show when={draft().discovery_path && !short()} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate>证据目录 · {draft().discovery_path}</text></Show>
        </box>
      </box>
    </scrollbox>
  </box>;
}

export default GoalView;
