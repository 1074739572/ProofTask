import {For, Show, createMemo} from 'solid-js';
import {formatStageElapsed, goalPhaseOf, normalizeGoalStage, type GoalDraftTaskSummary, type GoalTaskSnapshot, type StageMark} from './goal-state.ts';
import {
  GOAL_TRACK,
  fallbackGoal,
  goalPhaseLabel,
  goalPhaseTrackIndex,
  goalStatusColor,
  goalStatusIcon,
  goalStatusLabel,
  goalTaskColor,
  goalTaskIcon,
  goalTaskState,
  isSnapshot,
  readSource,
  type GoalLike,
} from './goal-presentation.ts';
import {C} from './theme.ts';
import {clipTerminalText, layoutMode, terminalColumns, type LayoutMode} from './layout.ts';

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
  /** Terminal width used to keep the first screen readable on narrow TTYs. */
  width?: number | (() => number);
  /** Available main-view height; short terminals need a stricter summary. */
  height?: number | (() => number);
  /** Animation frame counter (~80ms). Drives the progress-bar head pulse;
   * optional so off-screen previews stay static. */
  tick?: () => number;
  /** 客户端阶段时钟的切换记录；用于执行链路的每阶段耗时。 */
  stageMarks?: StageMark[] | (() => StageMark[]);
  /** 当前时间；缺省 Date.now()。调试预览传入固定值以得到确定性耗时。 */
  now?: number | (() => number);
};

type TaskBoardRow = {icon: string; color: string; subject: string; note: string};

function safeText(value: unknown, fallback = '暂无'): string {
  const text = value == null ? fallback : String(value).trim();
  return text || fallback;
}

function clip(value: unknown, width: number): string {
  return clipTerminalText(safeText(value), width);
}

function statusOf(goal: GoalLike): string {
  return safeText(goal.status, 'unknown');
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
  if (permissionIsWaiting(goal)) return '批准后继续（/goal resume）';
  if (status === 'paused') return '恢复当前 Goal（/goal resume）';
  if (status === 'failed') return '查看失败详情（/goal status）';
  if (status === 'ready') return '开始执行（/goal start）';
  if (status === 'completed' || status === 'consumed') return '查看结果（/goal status）';
  return '暂停以调整（/goal pause）';
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

function taskBoardFor(goal: GoalLike): TaskBoardRow[] {
  if (!isSnapshot(goal)) return [];
  return goal.tasks.map(task => {
    const state = goalTaskState(task, goal.current_task_id);
    const blocked = state === 'pending' && (task.blocked_by || []).length > 0;
    const note = state === 'done'
      ? (task.evidence_count ? `证据 ${task.evidence_count}` : '完成')
      : state === 'failed' ? '失败'
        : state === 'active' ? '进行中'
          : blocked ? '等前序完成'
            : '';
    return {
      icon: goalTaskIcon(state),
      color: goalTaskColor(state),
      subject: safeText(task.subject),
      note,
    };
  });
}

export function GoalSummary(props: GoalSummaryProps) {
  const goal = () => readSource(props.goal) || fallbackGoal();
  const decisions = () => readSource(props.decisions) || [];
  const status = () => statusOf(goal());
  const phase = () => goalPhaseOf(goal()) || 'intake';
  const currentTask = () => taskFor(goal());
  // paused/failed/cancelled 时轨道索引为 -1：时间线不高亮任何阶段，
  // 避免把「需求」或「验证」错标为活动阶段（resume_phase 能定位时除外）。
  const activePhase = () => {
    const current = goal();
    return goalPhaseTrackIndex(phase(), isSnapshot(current) ? current.resume_phase : undefined);
  };
  const permissionWaiting = () => permissionIsWaiting(goal());
  const displayStatus = () => permissionWaiting() ? 'permission_wait' : status();
  const showRecovery = () => status() === 'paused' || status() === 'failed';
  const width = () => Math.max(1, Number(readSource(props.width)) || 120);
  const height = () => Math.max(1, Number(readSource(props.height)) || 28);
  const mode = (): LayoutMode => layoutMode(width(), height());
  const narrow = () => mode() !== 'wide';
  const short = () => mode() === 'short';
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
  const taskBoard = () => taskBoardFor(goal());
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
  const nowValue = () => {
    const source = readSource(props.now);
    return typeof source === 'number' ? source : Date.now();
  };
  // 阶段耗时：取该阶段最后一次进入的时间戳，到下一阶段切换（或现在）。
  // marks 来自客户端时钟，未观察到的阶段不显示耗时。
  // 归一化与末次索引只随 stageMarks 变化（动画时钟 12fps 下不再每帧重扫数组）；
  // 耗时本身随 now 走，留在查找时计算。
  const stageIndex = createMemo(() => {
    const marks = (readSource(props.stageMarks) || [])
      .map(mark => ({phase: normalizeGoalStage(mark.phase), at: mark.at}));
    const lastIndex = new Map<string, number>();
    marks.forEach((mark, index) => lastIndex.set(mark.phase, index));
    return {marks, lastIndex};
  });
  const stageElapsed = (key: string): string => {
    const {marks, lastIndex} = stageIndex();
    const idx = lastIndex.get(key);
    if (idx == null) return '';
    const start = marks[idx].at;
    const end = marks[idx + 1]?.at ?? nowValue();
    return formatStageElapsed(Math.max(0, Math.round((end - start) / 1000)));
  };
  const roundsInfo = createMemo(() => {
    const current = goal();
    if (!isSnapshot(current)) return null;
    const used = current.total_llm_rounds || 0;
    const max = current.max_total_rounds || 0;
    const cycles = current.task_cycles || 0;
    const rollovers = current.worker_rollovers || 0;
    if (!used && !max && !cycles && !rollovers) return null;
    return {used, max, cycles, rollovers};
  });
  const roundCells = () => roundsInfo() && (roundsInfo()?.max || 0) > 0
    ? Math.max(0, Math.min(10, Math.round((roundsInfo()?.used || 0) / (roundsInfo()?.max || 1) * 10)))
    : 0;
  // 执行链路的分段视图。耗时是加分项：超出预算时按
  // 「最旧已完成阶段 → 全部已完成 → 仅活动阶段 → 无」逐级丢弃，保证不溢出卡片。
  const timelineCells = (): {text: string; color: string}[] => {
    const cells = GOAL_TRACK.map(([key, label], index) => ({
      key, label, elapsed: stageElapsed(key),
      state: index < activePhase() ? 'done' as const : index === activePhase() ? 'active' as const : 'pending' as const,
    }));
    const icon = (state: string) => state === 'active' ? '◉' : state === 'done' ? '●' : '○';
    const color = (state: string) => state === 'active' ? C.primary : state === 'done' ? C.success : C.textMuted;
    const budget = Math.max(24, Math.floor(width() * (mode() === 'wide' ? 0.64 : 1)) - 6);
    const overhead = terminalColumns('执行链路 ') + Math.max(0, cells.length - 1) * 3
      + cells.reduce((sum, cell) => sum + terminalColumns(`${icon(cell.state)}${cell.label}`), 0);
    const elapsedWidth = (keep: (cell: typeof cells[number]) => boolean) => overhead + cells.reduce(
      (sum, cell) => keep(cell) && cell.elapsed ? sum + 1 + cell.elapsed.length : sum, 0);
    const doneWithElapsed = cells.filter(cell => cell.state === 'done' && cell.elapsed);
    let keep: (cell: typeof cells[number]) => boolean = () => true;
    if (elapsedWidth(keep) > budget) {
      const dropped: string[] = [];
      while (doneWithElapsed.length && elapsedWidth(cell => !dropped.includes(cell.key)) > budget) {
        dropped.push(doneWithElapsed.shift()!.key);
      }
      const droppedSet = new Set(dropped);
      keep = cell => !droppedSet.has(cell.key);
      if (elapsedWidth(keep) > budget) {
        keep = cell => cell.state === 'active';
        if (elapsedWidth(keep) > budget) keep = () => false;
      }
    }
    return cells.map(cell => ({
      text: `${icon(cell.state)}${cell.label}${keep(cell) && cell.elapsed ? ` ${cell.elapsed}` : ''}`,
      color: color(cell.state),
    }));
  };

  const statusText = () => `${goalStatusIcon(displayStatus())} ${goalStatusLabel(displayStatus())}`;
  const progressText = () => progress().total ? `${progress().done}/${progress().total} 任务 · ${progress().percent}%` : '任务图准备中';
  // The compact shell renders its own one-line details affordance directly
  // below the summary.  Keep the extra hint only where it adds information:
  // short terminals need the explicit key, while wide terminals benefit from
  // the longer mouse/keyboard wording beside the inspector.
  const detailsHint = () => short() ? '[Enter] 查看任务 / Agent / 证据' : '';

  // A compact execution summary replaces the former three-card row.  The
  // fields are ordered by actionability so short terminals lose IDs and
  // decoration before they lose the current action or recovery command.
  return <box flexDirection="column" flexGrow={0} flexShrink={0} minWidth={0} paddingX={1} paddingTop={short() ? 0 : 1}>
    <box border={!short()} borderStyle="rounded" borderColor={goalStatusColor(displayStatus())} flexDirection="column" flexShrink={0} paddingX={short() ? 0 : 1} minWidth={0}>
      <box flexDirection={mode() === 'wide' ? 'row' : 'column'} justifyContent="space-between" minWidth={0} flexShrink={1}>
        <text fg={C.primary} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{short() ? 'GOAL' : `GOAL · ${clip(goal().target, mode() === 'wide' ? 74 : 54)}`}</text>
        <text fg={goalStatusColor(displayStatus())} wrapMode="none" truncate flexShrink={0}>{statusText()}</text>
      </box>
      <Show when={!short()} fallback={<box />}>
        <text fg={C.textMuted} wrapMode="none" truncate flexShrink={1}>{mode() === 'wide' ? `ID ${clip(goal().id, 52)} · ` : ''}阶段 {goalPhaseLabel(phase())}</text>
      </Show>
      <Show when={short()} fallback={<box />}><text fg={C.text} wrapMode="none" truncate>{clip(goal().target, 48)}</text></Show>
      <box flexDirection="row" minWidth={0} marginTop={narrow() ? 0 : 1}>
        <text fg={C.success} wrapMode="none" flexShrink={0}>{'━'.repeat(barFilled())}</text>
        <text fg={C.primary} wrapMode="none" flexShrink={0}>{barHead()}</text>
        <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{barRest()}</text>
        <text fg={C.text} wrapMode="none" truncate flexGrow={1} flexShrink={1}>  {progressText()}</text>
      </box>
      <Show when={roundsInfo()} fallback={<box />}>
        <box flexDirection="row" minWidth={0} marginTop={narrow() ? 0 : 1}>
          <text fg={C.textMuted} wrapMode="none" flexShrink={0}>轮次 </text>
          <Show when={(roundsInfo()?.max || 0) > 0} fallback={<box />}>
            <text fg={(roundsInfo()?.used || 0) >= (roundsInfo()?.max || 0) ? C.error : C.primary} wrapMode="none" flexShrink={0}>{'█'.repeat(roundCells())}</text>
            <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{'░'.repeat(Math.max(0, 10 - roundCells()))}</text>
          </Show>
          <text fg={C.text} wrapMode="none" truncate flexShrink={1}>{` ${roundsInfo()?.used || 0}${(roundsInfo()?.max || 0) > 0 ? `/${roundsInfo()?.max}` : ''} 轮`}</text>
          <Show when={(roundsInfo()?.cycles || 0) > 0} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate> · 循环 {roundsInfo()?.cycles}</text></Show>
          <Show when={(roundsInfo()?.rollovers || 0) > 0} fallback={<box />}><text fg={C.warning} wrapMode="none" truncate> · worker 重置 {roundsInfo()?.rollovers}</text></Show>
        </box>
      </Show>
      <Show when={!short()} fallback={<box />}>
        <box flexDirection="row" minWidth={0} marginTop={narrow() ? 0 : 1}>
          <text fg={C.secondary} wrapMode="none" flexShrink={0}>执行链路 </text>
          <For each={timelineCells()}>{(cell, index) => <>
            <Show when={index() > 0} fallback={<box />}>
              <text fg={C.textMuted} wrapMode="none" flexShrink={0}>{' › '}</text>
            </Show>
            <text fg={cell.color} wrapMode="none" flexShrink={0} selectable={false}>{cell.text}</text>
          </>}</For>
        </box>
      </Show>
    </box>

    <Show when={taskBoard().length > 0} fallback={
      <box flexDirection="column" minWidth={0} flexShrink={0} marginTop={narrow() ? 0 : 1}>
        <text fg={C.info} wrapMode="none" truncate>Task  {clip(taskLabel(), metricWidth())}</text>
        <text fg={C.secondary} wrapMode="none" truncate>Agent {clip(currentAgentFor(goal(), decisions()), metricWidth())}</text>
      </box>
    }>
      <box flexDirection="column" minWidth={0} flexShrink={0} marginTop={narrow() ? 0 : 1}>
        <text fg={C.secondary} wrapMode="none" truncate>任务看板 {progress().done}/{progress().total} 完成</text>
        <For each={taskBoard()}>{row => <box flexDirection="row" minWidth={0}>
          <text fg={row.color} wrapMode="none" flexShrink={0} selectable={false}>{`${row.icon} `}</text>
          <text fg={row.color} wrapMode="none" truncate flexGrow={1} flexShrink={1}>{row.subject}</text>
          <Show when={row.note} fallback={<box />}>
            <text fg={C.textMuted} wrapMode="none" truncate flexShrink={0}> · {row.note}</text>
          </Show>
        </box>}</For>
      </box>
    </Show>
    <text fg={permissionWaiting() ? C.warning : C.success} wrapMode="none" truncate marginTop={narrow() ? 0 : 1}>下一步 {clip(nextAction(goal()), metricWidth())}</text>

    <Show when={permissionWaiting() || showRecovery()} fallback={<box />}>
      <Show when={permissionWaiting()} fallback={<box />}>
        <text fg={C.warning} wrapMode="none" truncate>权限：等待工具权限批准，批准后可恢复</text>
      </Show>
      <Show when={showRecovery()} fallback={<box />}><text fg={C.warning} wrapMode="word" truncate>状态说明：{permissionWaiting() ? '等待批准后可恢复执行' : clip(errorFor(goal()), 80)}</text></Show>
    </Show>
    <Show when={detailsHint()} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate selectable={false}>{detailsHint()}</text></Show>
  </box>;
}

export default GoalSummary;
