import {createMemo, createRenderEffect, createSignal, For, Show} from 'solid-js';
import {useKeyboard, useRenderer} from '@opentui/solid';
import {GoalDetails} from './GoalDetails.tsx';
import {GoalSummary} from './GoalSummary.tsx';
import {createStageClock, goalPhaseOf, type GoalDraftSnapshot, type GoalSnapshot, type StageMark} from './goal-state.ts';
import {
  GOAL_SIDE_COLUMN_RATIO,
  fallbackGoal,
  goalDraftAgentRows,
  goalDraftHeartbeatPresentation,
  goalDraftNextActionPresentation,
  goalDraftStageRail,
  isSnapshot,
  readSource,
} from './goal-presentation.ts';
import type {InteractionTrace} from './interaction-trace.ts';
import {submitRenderFrame} from './interaction-trace.ts';
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

/**
 * 常驻执行流：右栏（宽屏）或摘要下方（窄屏）展示每个模型正在做什么。
 * 活跃 Agent 头行 + 当前意图 + 最近工具行；监督模型单独一行。
 * compact 模式（堆叠且高度不足）：每个模型合并为单行（意图并入头行），
 * 省略工具明细与块间距，监督模型也压成单行，保证不超出主区高度。
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
  const labelWidth = () => Math.max(16, Math.floor(props.width() * (props.fullWidth() ? 0.96 : GOAL_SIDE_COLUMN_RATIO)) - 4);
  const supervision = () => {
    const current = props.goal();
    return isSnapshot(current) ? current.supervision : undefined;
  };
  const supervisorStatus = () => supervision()?.status;
  const supervisorTone = () =>
    supervisorStatus() === 'unavailable' ? C.error : supervisorStatus() === 'attention' ? C.warning : C.info;
  const supervisorIcon = () =>
    supervisorStatus() === 'unavailable' ? '×' : supervisorStatus() === 'attention' ? '!' : '●';
  const toolRows = (tools: readonly unknown[]): FlowTool[] => (
    props.compact() ? [] : (tools as FlowTool[]).slice(-3)
  );
  const shown = () => props.decisions().slice(-4);
  // 计数口径是「不同的模型/Agent」，同一模型连发多条决策只算一个。
  const modelCount = () => new Set(shown().map(decision => decision.agent || 'Agent')).size + (supervision() ? 1 : 0);
  const supervisorLine = () => {
    const label = `监督 · ${supervisorStatusLabel(supervision()?.status)}`;
    if (!props.compact()) return `${label}${supervision()?.model ? ` · ${supervision()?.model}` : ''}`;
    const latest = supervision()?.latest;
    const detail = latest
      ? `${latest.action || ''}${latest.summary ? `：${latest.summary}` : (latest.next_step || '')}`
      : '';
    return clipTerminalText(detail ? `${label} · ${detail}` : label, labelWidth());
  };

  return <box flexDirection="column" minWidth={0} minHeight={0} flexGrow={1} flexShrink={1} paddingX={1}>
    {/* 布局确定性：不用 marginTop/paddingTop（OpenTUI 会把它算进文本高度，
        高度临界时产生 h=2 的错位测量），改用显式高度 1 的 spacer 盒。 */}
    <Show when={!props.compact()} fallback={<box height={0} flexShrink={0} />}>
      <box height={1} flexShrink={0} />
    </Show>
    <text fg={C.secondary} wrapMode="none" truncate flexShrink={0}>执行流 · {modelCount()} 个模型</text>
    <For each={shown()}>{decision => {
      const active = () => decision.status === 'active';
      const failed = () => decision.status === 'failed';
      const tone = () => active() ? C.info : failed() ? C.error : C.textMuted;
      const header = () => props.compact()
        ? clipTerminalText(
          `${decision.agent || 'Agent'}${active() && decision.round ? ` · 第 ${decision.round} 轮` : ''}${decision.text ? ` · ${decision.text}` : ''}`,
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
    <Show when={supervision()} fallback={<box height={0} flexShrink={0} />}>
      <box flexDirection="column" minWidth={0} flexShrink={0}>
        <box flexDirection="row" minWidth={0} flexShrink={0}>
          <text fg={supervisorTone()} wrapMode="none" flexShrink={0} selectable={false}>{`${supervisorIcon()} `}</text>
          <text fg={supervisorTone()} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{supervisorLine()}</text>
        </box>
        <Show when={!props.compact() && (supervision()?.latest?.summary || supervision()?.latest?.action || supervision()?.latest?.next_step)} fallback={<box height={0} flexShrink={0} />}>
          <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>{clipTerminalText(
            `  ${supervision()?.latest?.action || ''}${supervision()?.latest?.summary ? `：${supervision()?.latest?.summary}` : (supervision()?.latest?.next_step || '')}${relativeTime(supervision()?.latest?.at, props.now()) ? ` · ${relativeTime(supervision()?.latest?.at, props.now())}` : ''}`,
            labelWidth(),
          )}</text>
        </Show>
      </box>
    </Show>
    <Show when={shown().length === 0 && !supervision()} fallback={<box height={0} flexShrink={0} />}>
      <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}>暂无模型执行事件</text>
    </Show>
  </box>;
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
    const next = !before;
    props.interactionTrace?.record({event: 'state_before', target: 'GOAL_DETAILS_TOGGLE', state_before: {expanded: before}});
    setDetailsExpanded(next);
    props.interactionTrace?.record({event: 'state_after', target: 'GOAL_DETAILS_TOGGLE', state_after: {expanded: next}});
    // 帧提交由 signal 的同步响应式 effect 负责，避免早于详情分支更新。
  };

  useKeyboard((event: any) => {
    if (props.composerEmpty && !props.composerEmpty()) return;
    if (event?.ctrl || event?.meta || event?.alt) return;
    const key = String(event?.name || event?.key || '').toLowerCase();
    // 不用字母键（如 d）：空输入框时按键应流入 composer，否则用户无法输入
    // 以该字母开头的消息。Enter 在空输入框本就无效，Space 开头的消息会被
    // 提交前 trim，因此两者作为展开/收起快捷键是安全的。
    if (key === 'enter' || key === 'return' || key === 'space' || key === ' ') {
      toggleDetails();
      event.preventDefault?.();
    }
  });

  // 客户端阶段时钟：phase 切换即记时。调试场景允许注入固定记录。
  const internalClock = createStageClock(() => goalPhaseOf(selectedGoal()));
  const resolvedStageMarks = createMemo<StageMark[]>(() =>
    (readSource(props.stageMarks) as StageMark[] | undefined) ?? internalClock());
  const frame = () => (typeof props.tick === 'function' ? (props.tick as () => number)() : 0);
  const now = () => (typeof props.now === 'function' ? (props.now as () => number)() : Date.now());
  const layoutModeOf = (): LayoutMode => layoutMode(viewWidth(), viewHeight());
  const isWide = () => layoutModeOf() === 'wide';
  const isShort = () => layoutModeOf() === 'short';
  // 堆叠布局的行数预算：优先用 App 按 layoutBudget 扣除 composer/页脚/队列
  // 后的真实预算；缺省（debug 预览）按「状态行 2 + composer 3」固定开销估算。
  const mainRows = () => {
    const provided = readSource(props.mainRows);
    return typeof provided === 'number' && Number.isFinite(provided)
      ? Math.max(0, provided)
      : Math.max(0, viewHeight() - 5);
  };
  const summaryRows = () => {
    const tasks = (selectedGoal().tasks || []).length;
    const boxRows = isShort() ? 4 : isWide() ? 10 : 8;
    const board = tasks > 0 ? 1 + tasks : 2;
    return boxRows + board + 2;
  };
  // 执行流降级：宽屏恒为 full；堆叠时按剩余高度选 full → compact（单行/模型）
  // → none。行数按真实渲染行计数（spacer+头行+意图+工具+监督两行+详情开关）。
  const flowVariant = (): 'full' | 'compact' | 'none' => {
    if (isWide()) return 'full';
    if (isShort()) return 'none';
    const decisions = (readSource(props.decisions) || []).slice(-4);
    const goal = selectedGoal();
    const snapshot = 'phase' in goal ? goal : undefined;
    const supervised = !!snapshot?.supervision;
    const supervisorFullRows = snapshot?.supervision ? (snapshot.supervision.latest ? 2 : 1) : 0;
    const modelFullRows = decisions.reduce((sum, decision) => sum + 1
      + (decision.status === 'active' && decision.text ? 1 : 0)
      + Math.min(3, (decision.tools || []).length), 0);
    const remaining = mainRows() - summaryRows();
    if (remaining >= 2 + modelFullRows + supervisorFullRows + 1) return 'full';
    if (remaining >= 1 + decisions.length + (supervised ? 1 : 0) + 1) return 'compact';
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
  const details = <GoalDetails
    goal={selectedGoal}
    expanded={detailsExpanded}
    onToggle={toggleDetails}
    interactionTrace={props.interactionTrace}
    width={viewWidth}
    height={viewHeight}
  />;
  return <box flexDirection={isWide() ? 'row' : 'column'} flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} height="100%">
    <box flexDirection="column" width={isWide() ? `${(1 - GOAL_SIDE_COLUMN_RATIO) * 100}%` : '100%'} flexGrow={0} flexShrink={1} minHeight={0} minWidth={0}>{summary}</box>
    <box flexDirection="column" width={isWide() ? `${GOAL_SIDE_COLUMN_RATIO * 100}%` : '100%'} flexGrow={isWide() ? 1 : 0} flexShrink={1} minHeight={0} minWidth={0}>
      <Show when={flowVariant() !== 'none'} fallback={<box />}>
        {executionFlow}
      </Show>
      {details}
    </box>
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
