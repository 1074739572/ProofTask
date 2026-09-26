import {createMemo, For, Show} from 'solid-js';
import {GoalSummary} from './GoalSummary.tsx';
import {createStageClock, formatStageElapsed, goalPhaseOf, normalizeGoalStage, type GoalDraftSnapshot, type GoalSnapshot, type GoalTaskSnapshot, type StageMark} from './goal-state.ts';
import {
  GOAL_MAIN_COLUMN_RATIO,
  GOAL_SIDE_COLUMN_RATIO,
  GOAL_TRACK,
  GOAL_UI_LABELS,
  fallbackGoal,
  goalDraftAgentRows,
  goalDraftHeartbeatPresentation,
  goalDraftNextActionPresentation,
  goalDraftStageRail,
  goalStatusColor,
  goalTaskColor,
  goalTaskIcon,
  goalTaskState,
  isSnapshot,
  readSource,
} from './goal-presentation.ts';
import type {InteractionTrace} from './interaction-trace.ts';
import {clipTerminalText, layoutMode, type LayoutMode} from './layout.ts';
import {C} from './theme.ts';

export * from './goal-state.ts';
export * from './goal-presentation.ts';

export type GoalDecision = {id?: string; runId?: string; phase?: string; agent?: string; model?: string; text?: string; status?: string; at?: number; startedAt?: number; elapsed?: number; round?: number; tools?: unknown[]};

type GoalLike = GoalSnapshot | GoalDraftSnapshot;
type GoalSource = GoalLike | (() => GoalLike | null | undefined);
type DraftSource = GoalDraftSnapshot | (() => GoalDraftSnapshot | null | undefined);
type DecisionsSource = readonly GoalDecision[] | (() => readonly GoalDecision[] | undefined);

export type GoalViewProps = {
  goal?: GoalSource;
  draft?: DraftSource;
  snapshot?: GoalSource;
  decisions?: DecisionsSource;
  interactionTrace?: InteractionTrace;
  width?: number | (() => number);
  height?: number | (() => number);
  /** 主区可用行数（扣除 composer/页脚/队列后的预算）；缺省按 height - 5 估算。 */
  mainRows?: number | (() => number);
  composerEmpty?: () => boolean;
  /** 动画帧计数（~80ms）；驱动执行流 spinner。 */
  tick?: () => number;
  /** 当前时间；缺省 Date.now()。调试预览传入固定值以得到确定性耗时。 */
  now?: number | (() => number);
  /** 调试用：注入固定阶段时钟记录；缺省时使用内置客户端时钟。 */
  stageMarks?: StageMark[] | (() => StageMark[]);
  [key: string]: unknown;
};

type FlowTool = {id?: string; name?: string; summary?: string; status?: string};

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

function supervisorStatusLabel(status: string | undefined): string {
  if (status === 'attention') return '需关注';
  if (status === 'unavailable') return '不可用';
  if (status === 'observing') return '观察中';
  return status || '等待事件';
}

function relativeTime(at: number | undefined, now: number): string {
  if (!at || !Number.isFinite(at) || at <= 0) return '';
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  return `${Math.floor(seconds / 3600)}h 前`;
}

function elapsedText(startedAt: number | undefined, now: number): string {
  if (!startedAt || !Number.isFinite(startedAt) || startedAt <= 0) return '';
  const seconds = Math.max(0, Math.round((now - startedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  return `${Math.floor(seconds / 3600)}h${String(Math.round((seconds % 3600) / 60)).padStart(2, '0')}m`;
}

/**
 * 常驻执行流：左栏展示执行 Agent（不含监督模型）。
 * 活跃 Agent 头行 + 当前意图 + 最近工具行。
 * compact 模式（堆叠且高度不足）：每个模型合并为单行，
 * 省略工具明细与块间距，保证不超出主区高度。
 */
function GoalExecutionFlow(props: {
  goal: () => GoalLike;
  decisions: () => readonly GoalDecision[];
  frame: () => number;
  now: () => number;
  width: () => number;
  compact: () => boolean;
  fullWidth: () => boolean;
}) {
  const spinner = () => SPINNER_FRAMES[Math.abs(props.frame()) % SPINNER_FRAMES.length];
  const labelWidth = () => Math.max(16, Math.floor(props.width() * (props.fullWidth() ? 0.96 : GOAL_MAIN_COLUMN_RATIO)) - 4);
  const toolRows = (tools: readonly unknown[]): FlowTool[] => (
    props.compact() ? [] : (tools as FlowTool[]).slice(-3)
  );
  const shown = () => props.decisions().slice(-4);
  const modelCount = () => new Set(shown().map(decision => decision.agent || 'Agent')).size;

  return <box flexDirection="column" minWidth={0} minHeight={0} flexGrow={props.fullWidth() ? 0 : 1} flexShrink={props.fullWidth() ? 0 : 1} paddingX={1}>
    <Show when={!props.compact()} fallback={<box height={0} flexShrink={0} />}>
      <box height={1} flexShrink={0} />
    </Show>
    <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.executionFlow} · {modelCount()} 个模型</text>
    <For each={shown()}>{decision => {
      const active = () => decision.status === 'active';
      const failed = () => decision.status === 'failed';
      const tone = () => active() ? C.info : failed() ? C.error : C.textMuted;
      const timeText = () => {
        if (active() && decision.startedAt) return elapsedText(decision.startedAt, props.now());
        if (!active() && decision.at) return relativeTime(decision.at, props.now());
        return '';
      };
      const header = () => props.compact()
        ? clipTerminalText(
          `${decision.agent || 'Agent'}${active() && decision.round ? ` · 第 ${decision.round} 轮` : ''}${decision.text ? ` · ${decision.text}` : ''}${timeText() ? ` · ${timeText()}` : ''}`,
          labelWidth(),
        )
        : clipTerminalText(
          `${decision.agent || 'Agent'}${decision.model ? ` · ${decision.model}` : ''}`
          + `${active() && decision.round ? ` · 第 ${decision.round} 轮` : ''}`
          + `${!active() && decision.text ? ` · ${decision.text}` : ''}`,
          labelWidth(),
        );
      return <box flexDirection="column" minWidth={0} flexShrink={0}>
        <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={tone()} wrapMode="none" flexShrink={0} selectable={false}>{`${active() ? spinner() : failed() ? '×' : '✓'} `}</text>
          <text fg={tone()} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{header()}</text>
          <Show when={timeText() && !props.compact()} fallback={<box />}>
            <text fg={active() ? C.info : C.textMuted} wrapMode="none" flexShrink={0}>{timeText()}</text>
          </Show>
        </box>
        <Show when={!props.compact() && active() && decision.text} fallback={<box height={0} flexShrink={0} />}>
          <text fg={C.text} wrapMode="none" truncate flexShrink={0}>{clipTerminalText(`  ${decision.text}`, labelWidth())}</text>
        </Show>
        <For each={toolRows(decision.tools || [])}>{tool => <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={tool.status === 'failed' ? C.error : tool.status === 'running' ? C.warning : C.success} wrapMode="none" flexShrink={0} selectable={false}>{`${tool.status === 'running' ? spinner() : tool.status === 'failed' ? '×' : '✓'} `}</text>
          <text fg={C.textMuted} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{clipTerminalText(`${String(tool.name || 'tool').replace(/_/g, ' ')}  ${tool.summary || ''}`, labelWidth())}</text>
        </box>}</For>
      </box>;
    }}</For>
    <Show when={shown().length === 0} fallback={<box height={0} flexShrink={0} />}>
      <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.noEvents}</text>
    </Show>
  </box>;
}

/** 当前任务的测试案例与验证命令。 */
function GoalTestPanel(props: {goal: () => GoalLike; width: () => number; variant: () => 'full' | 'compact' | 'none'}) {
  const goal = () => props.goal();
  const currentTask = () => {
    const g = goal();
    if (!isSnapshot(g)) return undefined;
    return g.tasks.find(task => task.id === g.current_task_id);
  };
  const cases = () => {
    const task = currentTask();
    return task && 'acceptance_cases' in task ? task.acceptance_cases || [] : [];
  };
  const command = () => {
    const task = currentTask();
    return task && 'verification_spec' in task ? task.verification_spec?.command : undefined;
  };
  const maxCases = () => props.variant() === 'full' ? 3 : 1;
  const labelWidth = () => Math.max(16, Math.floor(props.width() * GOAL_MAIN_COLUMN_RATIO) - 6);
  return <Show when={props.variant() !== 'none' && currentTask() && (cases().length > 0 || command())} fallback={<box />}>
    <box flexDirection="column" minWidth={0} flexShrink={0} paddingX={1} paddingTop={1}>
      <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.testCases} {cases().length ? `· ${GOAL_UI_LABELS.acceptances} ${cases().length} 项` : ''}</text>
      <For each={cases().slice(0, maxCases())}>{(item, index) => <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>  {index() + 1}. {clipTerminalText(`${item.given || ''} ${item.when || ''} ${item.then || ''}`.replace(/\s+/g, ' ').trim(), labelWidth())}</text>}</For>
      <Show when={command() && props.variant() === 'full'} fallback={<box />}>
        <text fg={C.info} wrapMode="none" truncate flexShrink={0}>  $ {clipTerminalText(command(), labelWidth())}</text>
      </Show>
    </box>
  </Show>;
}

/** 当前任务的模型检查过程：验证证据。 */
function GoalCheckPanel(props: {goal: () => GoalLike; width: () => number; now: () => number; variant: () => 'full' | 'compact' | 'none'}) {
  const goal = () => props.goal();
  const currentTask = () => {
    const g = goal();
    if (!isSnapshot(g)) return undefined;
    return g.tasks.find(task => task.id === g.current_task_id);
  };
  const evidence = () => {
    const task = currentTask();
    return task && 'latest_evidence' in task ? task.latest_evidence : undefined;
  };
  const labelWidth = () => Math.max(16, Math.floor(props.width() * GOAL_MAIN_COLUMN_RATIO) - 6);
  return <Show when={props.variant() !== 'none' && currentTask() && evidence()} fallback={<box />}>
    <box flexDirection="column" minWidth={0} flexShrink={0} paddingX={1} paddingTop={1}>
      <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.checkProcess} · {GOAL_UI_LABELS.currentTask}</text>
      <Show when={evidence()} fallback={<box />}>
        <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={evidence()?.exit_code === 0 ? C.success : C.error} wrapMode="none" flexShrink={0} selectable={false}>{evidence()?.exit_code === 0 ? '✓ ' : '× '}</text>
          <text fg={C.textMuted} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{clipTerminalText(`${GOAL_UI_LABELS.verify} 退出码 ${evidence()?.exit_code ?? '?'}${evidence()?.verified_by ? ` · ${GOAL_UI_LABELS.verifiedBy} ${evidence()?.verified_by}` : ''}`, labelWidth())}</text>
          <Show when={evidence()?.duration_ms && props.variant() === 'full'} fallback={<box />}>
            <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{Math.round((evidence()?.duration_ms || 0) / 1000)}s</text>
          </Show>
        </box>
      </Show>
    </box>
  </Show>;
}

/**
 * 右栏任务看板：独立的任务状态面板。
 */
function GoalTaskBoardPanel(props: {goal: () => GoalLike; short: () => boolean}) {
  const rows = () => {
    const g = props.goal();
    if (isSnapshot(g)) {
      return g.tasks.map((task: GoalTaskSnapshot) => {
        const state = goalTaskState(task, g.current_task_id);
        const blocked = state === 'pending' && (task.blocked_by || []).length > 0;
        const current = task.id === g.current_task_id;
        const note = state === 'done'
          ? (task.evidence_count ? `证据 ${task.evidence_count}` : '完成')
          : state === 'failed' ? '失败'
            : state === 'active' ? '进行中'
              : blocked ? '等前序完成' : '';
        let detail: string | undefined;
        if (current) {
          const parts: string[] = [];
          if (task.acceptance_cases?.length) parts.push(`验收 ${task.acceptance_cases.length} 项`);
          if (task.verification_spec?.command) parts.push(`$ ${task.verification_spec.command}`);
          detail = parts.length ? parts.join(' · ') : undefined;
        }
        return {icon: goalTaskIcon(state), color: goalTaskColor(state), subject: task.subject || '', note, current, detail};
      });
    }
    // Draft snapshot: tasks are GoalDraftTaskSummary (no status/verification)
    return (g.tasks || []).map(task => {
      const done = /^(done|completed|passing)$/i.test(task.name || '');
      return {
        icon: done ? '✓' : '○',
        color: done ? C.success : C.textMuted,
        subject: task.name || '',
        note: task.verification_source ? `验证 ${task.verification_source}` : '',
        current: false,
        detail: undefined,
      };
    });
  };
  const progress = () => {
    const g = props.goal();
    const tasks = g.tasks || [];
    const done = tasks.filter(t => {
      if ('status' in t) return /^(done|completed|passing)$/i.test(t.status);
      return false;
    }).length;
    return {done, total: tasks.length};
  };
  return <Show when={!props.short() && rows().length > 0} fallback={<box />}>
    <box border borderStyle="rounded" borderColor={C.secondary} flexDirection="column" flexShrink={0} marginTop={1} paddingX={1} paddingBottom={1} minWidth={0}>
      <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.taskBoard} · {progress().done}/{progress().total} 完成</text>
      <For each={rows()}>{row => <box flexDirection="column" minWidth={0} flexShrink={0}>
        <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={row.color} wrapMode="none" flexShrink={0} selectable={false}>{`${row.icon} `}</text>
          <text fg={row.color} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{row.subject}</text>
          <Show when={row.note} fallback={<box />}>
            <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}> · {row.note}</text>
          </Show>
        </box>
        <Show when={row.current && row.detail} fallback={<box />}>
          <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>  {row.detail}</text>
        </Show>
      </box>}</For>
    </box>
  </Show>;
}

/**
 * 右栏监督模型面板：全局监督状态和最近决策。
 */
function GoalSupervisionPanel(props: {
  goal: () => GoalLike;
  now: () => number;
  short: () => boolean;
}) {
  const supervision = () => {
    const current = props.goal();
    return isSnapshot(current) ? current.supervision : undefined;
  };
  const supervisorStatus = () => supervision()?.status;
  const tone = () =>
    supervisorStatus() === 'unavailable' ? C.error : supervisorStatus() === 'attention' ? C.warning : C.info;
  const icon = () =>
    supervisorStatus() === 'unavailable' ? '×' : supervisorStatus() === 'attention' ? '!' : '●';
  const stale = () => {
    const latest = supervision()?.latest;
    if (!latest?.at) return false;
    return props.now() - latest.at > 30_000;
  };
  const history = () => (supervision()?.history || []).slice(-3).reverse();
  return <Show when={!props.short() && supervision()} fallback={<box />}>
    <box border borderStyle="rounded" borderColor={C.warning} flexDirection="column" flexShrink={0} marginTop={1} paddingX={1} paddingBottom={1} minWidth={0}>
      <box flexDirection="row" minWidth={0} flexShrink={0}>
        <text fg={tone()} wrapMode="none" flexShrink={0} selectable={false}>{`${icon()} `}</text>
        <text fg={tone()} wrapMode="none" truncate flexGrow={1} flexShrink={1}>监督 · {supervisorStatusLabel(supervision()?.status)}{supervision()?.model ? ` · ${supervision()?.model}` : ''}</text>
        <Show when={stale()} fallback={<box />}>
          <text fg={C.warning} wrapMode="none" flexShrink={0}>可能停滞</text>
        </Show>
      </box>
      <Show when={supervision()?.latest?.summary || supervision()?.latest?.action || supervision()?.latest?.next_step} fallback={<box />}>
        <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>  {supervision()?.latest?.action || ''}{supervision()?.latest?.summary ? `：${supervision()?.latest?.summary}` : (supervision()?.latest?.next_step || '')}</text>
      </Show>
      <For each={history()}>{item => <box flexDirection="row" minWidth={0} flexShrink={0}>
        <text fg={C.info} wrapMode="none" flexShrink={0} selectable={false}>● </text>
        <text fg={C.textMuted} wrapMode="none" truncate flexGrow={1} flexShrink={1}>  #{history().length - history().indexOf(item)} {item.action || ''} → {item.summary || ''}</text>
        <Show when={item.at} fallback={<box />}>
          <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{relativeTime(item.at, props.now())}</text>
        </Show>
      </box>}</For>
    </box>
  </Show>;
}

/**
 * 右栏统计面板：总耗时、已用轮次、任务进度、各阶段耗时明细。
 * 阶段时钟数据来自客户端 stageMarks。
 */
function GoalStatsPanel(props: {
  goal: () => GoalLike;
  stageMarks: () => StageMark[];
  now: () => number;
  short: () => boolean;
}) {
  const data = createMemo(() => {
    const goal = props.goal();
    const marks = props.stageMarks();
    const now = props.now();

    let totalSec = 0;
    const stageMap = new Map<string, number>();

    if (marks && marks.length > 0) {
      const normalized = marks.map(m => ({phase: normalizeGoalStage(m.phase), at: m.at}));
      const lastIdx = new Map<string, number>();
      normalized.forEach((m, i) => lastIdx.set(m.phase, i));

      totalSec = Math.max(0, Math.round((now - marks[0].at) / 1000));

      for (const [key] of GOAL_TRACK) {
        const idx = lastIdx.get(key);
        if (idx == null) continue;
        const start = normalized[idx].at;
        const end = normalized[idx + 1]?.at ?? now;
        const sec = Math.max(0, Math.round((end - start) / 1000));
        if (sec > 0) stageMap.set(key, sec);
      }
    }

    const tasks = goal.tasks || [];
    const done = tasks.filter(t => 'status' in t && /^(done|completed|passing)$/i.test(t.status)).length;

    let roundsUsed = 0;
    let roundsMax = 0;
    if (isSnapshot(goal)) {
      roundsUsed = goal.total_llm_rounds || 0;
      roundsMax = goal.max_total_rounds || 0;
    }

    return {totalSec, stageMap, tasksDone: done, tasksTotal: tasks.length, roundsUsed, roundsMax};
  });

  const visibleStages = () => GOAL_TRACK.filter(([key]) => props.stageMarks().length > 0 && data().stageMap.has(key));

  return <Show when={!props.short() && data().totalSec > 0} fallback={<box />}>
    <box border borderStyle="rounded" borderColor={C.secondary} flexDirection="column" flexShrink={0} marginTop={1} paddingX={1} paddingBottom={1} minWidth={0}>
      <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>{GOAL_UI_LABELS.stats}</text>
      <text fg={C.text} wrapMode="none" truncate>{GOAL_UI_LABELS.totalElapsed}  {formatStageElapsed(data().totalSec)}</text>
      <Show when={data().roundsMax > 0} fallback={<box />}>
        <text fg={C.text} wrapMode="none" truncate>{GOAL_UI_LABELS.rounds}  {data().roundsUsed}/{data().roundsMax}</text>
      </Show>
      <Show when={data().tasksTotal > 0} fallback={<box />}>
        <text fg={C.text} wrapMode="none" truncate>{GOAL_UI_LABELS.taskProgress}  {data().tasksDone}/{data().tasksTotal}</text>
      </Show>
      <For each={visibleStages()}>
        {([key, label]) => <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={C.textMuted} wrapMode="none" truncate flexGrow={1}>{`  ${label}`}</text>
          <text fg={C.text} wrapMode="none" flexShrink={0}>{formatStageElapsed(data().stageMap.get(key) || 0)}</text>
        </box>}
      </For>
    </box>
  </Show>;
}

export function GoalView(props: GoalViewProps) {
  const viewWidth = () => Math.max(1, Number(readSource(props.width)) || 120);
  const viewHeight = () => Math.max(1, Number(readSource(props.height)) || 28);
  const selectedGoal = createMemo<GoalLike>(() => readSource(props.goal) || readSource(props.snapshot) || readSource(props.draft) || fallbackGoal());
  const internalClock = createStageClock(() => goalPhaseOf(selectedGoal()));
  const resolvedStageMarks = createMemo<StageMark[]>(() =>
    (readSource(props.stageMarks) as StageMark[] | undefined) ?? internalClock());
  const frame = () => (typeof props.tick === 'function' ? (props.tick as () => number)() : 0);
  const now = () => (typeof props.now === 'function' ? (props.now as () => number)() : Date.now());
  const layoutModeOf = (): LayoutMode => layoutMode(viewWidth(), viewHeight());
  const isWide = () => layoutModeOf() === 'wide';
  const isShort = () => layoutModeOf() === 'short';
  const mainRows = () => {
    const provided = readSource(props.mainRows);
    return typeof provided === 'number' && Number.isFinite(provided)
      ? Math.max(0, provided)
      : Math.max(0, viewHeight() - 5);
  };
  /** 宽屏右栏高度估算：任务看板 + 统计 + 监督 */
  const rightColRows = () => {
    const tasks = (selectedGoal().tasks || []).length;
    const board = tasks > 0 ? 2 + tasks : 2;
    const statsRows = isShort() ? 0 : 6;
    const supervisionRows = isShort() ? 0 : 5;
    return board + statsRows + supervisionRows;
  };
  /** 窄屏摘要区高度估算：仅 Goal 卡片（看板/统计/监督在执行流之后渲染） */
  const summaryRows = () => {
    if (isWide()) return 0;
    return isShort() ? 4 : 8;
  };
  const flowVariant = (): 'full' | 'compact' | 'none' => {
    if (isWide()) return 'full';
    if (isShort()) return 'none';
    const decisions = (readSource(props.decisions) || []).slice(-4);
    const goal = selectedGoal();
    const snapshot = 'phase' in goal ? goal : undefined;
    const supervisorFullRows = snapshot?.supervision ? (snapshot.supervision.latest ? 2 : 1) : 0;
    const modelFullRows = decisions.reduce((sum, decision) => sum + 1
      + (decision.status === 'active' && decision.text ? 1 : 0)
      + Math.min(3, (decision.tools || []).length), 0);
    const remaining = mainRows() - summaryRows();
    if (remaining >= 2 + modelFullRows + supervisorFullRows + 1) return 'full';
    if (remaining >= 1 + decisions.length + 1) return 'compact';
    return 'none';
  };
  // 测试/检查面板在堆叠布局下的降级：先砍检查历史行数，再砍测试案例数，最后隐藏。
  const testCheckVariant = (): 'full' | 'compact' | 'none' => {
    if (isWide()) return 'full';
    if (isShort()) return 'none';
    const goal = selectedGoal();
    if (!('phase' in goal)) return 'none';
    const snapshot = goal as GoalSnapshot;
    const currentTask = snapshot.tasks.find(t => t.id === snapshot.current_task_id);
    if (!currentTask) return 'none';
    const testRows = 1 + Math.min(3, (currentTask.acceptance_cases || []).length) + (currentTask.verification_spec?.command ? 1 : 0);
    const checkRows = 1 + (currentTask.latest_evidence ? 1 : 0);
    const remaining = mainRows() - summaryRows() - (flowVariant() === 'full' ? 8 : flowVariant() === 'compact' ? 4 : 0);
    if (remaining >= testRows + checkRows) return 'full';
    if (remaining >= 1 + 1 + 1 + 1) return 'compact';
    return 'none';
  };

  const summary = <GoalSummary
    goal={selectedGoal}
    decisions={() => readSource(props.decisions) || []}
    width={viewWidth}
    height={viewHeight}
    tick={typeof props.tick === 'function' ? props.tick as () => number : undefined}
    now={now}
    stageMarks={resolvedStageMarks}
    columnRatio={GOAL_MAIN_COLUMN_RATIO}
    showTaskBoard={false}
  />;
  const executionFlow = <GoalExecutionFlow
    goal={selectedGoal}
    decisions={() => readSource(props.decisions) || []}
    frame={frame}
    now={now}
    width={viewWidth}
    compact={() => flowVariant() === 'compact'}
    fullWidth={() => !isWide()}
  />;
  const testPanel = <GoalTestPanel goal={selectedGoal} width={viewWidth} variant={testCheckVariant} />;
  const checkPanel = <GoalCheckPanel goal={selectedGoal} width={viewWidth} now={now} variant={testCheckVariant} />;
  const taskBoardPanel = <GoalTaskBoardPanel goal={selectedGoal} short={() => isShort()} />;
  const stats = <GoalStatsPanel goal={selectedGoal} stageMarks={resolvedStageMarks} now={now} short={() => isShort()} />;
  const supervisionPanel = <GoalSupervisionPanel goal={selectedGoal} now={now} short={() => isShort()} />;
  return <box flexDirection={isWide() ? 'row' : 'column'} flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} height="100%">
    <box flexDirection="column" width={isWide() ? `${GOAL_MAIN_COLUMN_RATIO * 100}%` : '100%'} flexGrow={1} flexShrink={1} minHeight={0} minWidth={0}>
      {summary}
      <Show when={flowVariant() !== 'none'} fallback={<box />}>
        {executionFlow}
      </Show>
      <Show when={testCheckVariant() !== 'none'} fallback={<box />}>
        {testPanel}
        {checkPanel}
      </Show>
      {!isWide() && <>
        {taskBoardPanel}
        {stats}
        {supervisionPanel}
      </>}
    </box>
    {isWide() && <box flexDirection="column" width={`${GOAL_SIDE_COLUMN_RATIO * 100}%`} flexGrow={0} flexShrink={0} minHeight={0} minWidth={0}>
      {taskBoardPanel}
      {stats}
      {supervisionPanel}
    </box>}
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
  // 合并 DISCOVERY JOBS：文件数已并入各 Agent 行的 meta；没有 Agent 行的
  // job（如尚在排队的角色）以独立行补在列表尾部，避免信息丢失。
  const orphanJobs = () => {
    const labels = agents().map(row => row.label);
    return (draft().discovery_jobs || []).filter(job => {
      const labelsForRole: Record<string, string> = {architecture: '架构路径', implementation: '实现路径', tests: '测试路径', history: '历史路径'};
      const label = labelsForRole[job.role] || labelsForRole[job.role?.replace(/^goal_discovery_/, '')] || job.role;
      return !labels.includes(label);
    });
  };
  const discoveryDone = () => Number(draft().discovery_completed ?? 0);
  const discoveryTotal = () => Number(draft().discovery_total ?? draft().discovery_jobs?.length ?? 0);
  const discoveryProgress = () => discoveryTotal() > 0 ? `${discoveryDone()}/${discoveryTotal()} 个探索任务` : `${agents().length} 个 Agent`;
  const next = () => goalDraftNextActionPresentation(draft());
  const target = () => String(draft().target || '暂无 Goal 草稿').trim();
  const message = () => String(draft().message || draft().intake_summary || '').trim();
  const question = () => String(draft().question || '').trim();
  const options = () => Array.isArray(draft().question_options) ? draft().question_options!.filter(Boolean) : [];
  const questionDefault = () => String(draft().question_default || '').trim();

  return <box flexDirection="column" flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} height="100%">
    <box flexDirection="column" flexShrink={0} minWidth={0} paddingX={1} paddingTop={short() ? 0 : 1}>
      <box border={!short()} borderStyle="rounded" borderColor={statusColor(draft().status)} flexDirection="column" minWidth={0} paddingX={short() ? 0 : 1}>
        <box flexDirection={mode() === 'wide' ? 'row' : 'column'} justifyContent="space-between" minWidth={0}>
          <text fg={C.primary} wrapMode="none" truncate flexGrow={1}>DRAFT · {clip(target(), mode() === 'wide' ? 72 : 48)}</text>
          <text fg={statusColor(draft().status)} wrapMode="none" truncate>{statusLabel(draft().status)} · heartbeat {heartbeat().icon} {heartbeat().text}</text>
        </box>
        <Show when={short()} fallback={<box />}><text fg={C.text} wrapMode="none" truncate>{clip(target(), 48)}</text></Show>
        <text fg={C.secondary} wrapMode="none" truncate>{stageRail()}</text>
        <text fg={C.textMuted} wrapMode="none" truncate>{discoveryProgress()}{draft().verification ? ` · 验证 ${clip(draft().verification, 36)}` : ''}</text>
      </box>
      <Show when={message() && !short()} fallback={<box />}><text fg={C.textMuted} wrapMode="word" truncate marginTop={1}>{message()}</text></Show>
      <Show when={question()} fallback={<box />}>
        <box border borderStyle="rounded" borderColor={C.warning} flexDirection="column" minWidth={0} marginTop={1} paddingX={1}>
          <text fg={C.warning} wrapMode="none" truncate>! 需要回答</text>
          <text fg={C.text} wrapMode="word">{question()}</text>
          <For each={options()}>{(opt, i) => <text fg={C.text} wrapMode="none" truncate>{i() + 1}. {opt}</text>}</For>
          <Show when={questionDefault()} fallback={<box />}><text fg={C.textMuted} wrapMode="word" truncate>不答则按: {questionDefault()}</text></Show>
          <text fg={C.warning} wrapMode="none" truncate>Enter 回答{options().length ? '编号/选项' : ''} · /goal pause 暂停</text>
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
          <Show when={!short() && orphanJobs().length > 0} fallback={<box />}>
            <For each={orphanJobs()}>{job => <box flexDirection="row" minWidth={0} marginTop={1}>
              <text fg={statusColor(job.status)} wrapMode="none" flexShrink={0}>{job.status === 'done' ? '✓' : job.status === 'running' ? '●' : job.status === 'failed' ? '×' : '○'} </text>
              <text fg={C.text} wrapMode="none" truncate flexGrow={1}>{job.role} · {statusLabel(job.status)}</text>
              <text fg={C.textMuted} wrapMode="none" truncate>{job.read_path_count || 0} 个文件</text>
            </box>}</For>
          </Show>
        </Show>
        <Show when={!short() && (draft().intake_assumptions?.length || 0) > 0} fallback={<box />}>
          <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>ASSUMPTIONS</text>
          <For each={draft().intake_assumptions}>{item => <text fg={C.textMuted} wrapMode="word">· {item}</text>}</For>
        </Show>
        <box flexDirection="column" minWidth={0} marginTop={1}>
          <text fg={C.success} wrapMode="none" truncate>{next().command ? `${next().detail}（${next().command}）` : next().detail}</text>
          <Show when={draft().discovery_path && !short()} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate>证据目录 · {draft().discovery_path}</text></Show>
        </box>
      </box>
    </scrollbox>
  </box>;
}

export default GoalView;
