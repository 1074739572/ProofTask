import {For, Show} from 'solid-js';
import type {GoalDraftSnapshot, GoalSnapshot, GoalTaskSnapshot} from './goal-state.ts';
import {eventCoordinates, type InteractionTrace} from './interaction-trace.ts';
import {C} from './theme.ts';
import {clipTerminalText, layoutMode} from './layout.ts';

type GoalLike = GoalSnapshot | GoalDraftSnapshot;
type GoalSource = GoalLike | (() => GoalLike);
type DecisionLike = {
  agent?: string;
  model?: string;
  text?: string;
  status?: string;
  phase?: string;
  tools?: unknown[];
  elapsed?: number;
};
type DecisionsSource = readonly DecisionLike[] | (() => readonly DecisionLike[] | undefined);

export type GoalDetailsProps = {
  goal: GoalSource;
  expanded: boolean | (() => boolean);
  onToggle: () => void;
  interactionTrace?: InteractionTrace;
  width?: number | (() => number);
  height?: number | (() => number);
  decisions?: DecisionsSource;
};

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

function isSnapshot(goal: GoalLike): goal is GoalSnapshot {
  return 'phase' in goal;
}

/** 详情只把状态中的可安全呈现值转换为文本；缺失值由 Show 省略。 */
function present(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed || undefined;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized && serialized !== '{}' && serialized !== '[]' ? serialized : undefined;
  } catch {
    return undefined;
  }
}

function taskRows(goal: GoalLike): GoalTaskSnapshot[] {
  if (isSnapshot(goal)) return goal.tasks;
  return goal.tasks.map((task, index) => ({
    id: task.name || `draft-task-${index}`,
    subject: task.name,
    status: 'pending',
    verification_state: 'not_started',
    acceptance_cases: task.acceptance_count
      ? Array.from({length: task.acceptance_count}, (_, i) => ({id: `AC${i + 1}`, given: '', when: '', then: ''}))
      : undefined,
    primary_write: task.primary_write,
    planned_new: task.planned_new,
    test_strategy: task.test_strategy,
  }));
}

function detailLine(label: string, value: unknown): string | undefined {
  const safeValue = present(value);
  return safeValue === undefined ? undefined : `${label}：${safeValue}`;
}

function detailsForTask(task: GoalTaskSnapshot): string[] {
  const lines = [
    detailLine('任务', task.subject),
    detailLine('状态', task.status),
    `验收条件：${task.acceptance_cases?.length || 0} 项`,
    detailLine('Contract', task.verification_spec?.command),
    detailLine('Verification', task.test_strategy),
    detailLine('Evidence', task.latest_evidence?.stdout_tail),
    detailLine('Regression', task.evidence_count === undefined ? undefined : `${task.evidence_count} 条证据`),
    detailLine('Error', task.last_error),
  ];
  return lines.filter((line): line is string => typeof line === 'string' && line.length > 0);
}

function taskColor(task: GoalTaskSnapshot): string {
  if (task.verification_state === 'passing' || /^(done|completed)$/i.test(task.status)) return C.success;
  if (task.verification_state === 'failing' || task.status === 'failed') return C.error;
  if (/^(in_progress|active|running)$/i.test(task.status)) return C.primary;
  return C.textMuted;
}

function taskIcon(task: GoalTaskSnapshot): string {
  if (task.verification_state === 'passing' || /^(done|completed)$/i.test(task.status)) return '✓';
  if (task.verification_state === 'failing' || task.status === 'failed') return '×';
  if (/^(in_progress|active|running)$/i.test(task.status)) return '●';
  return '○';
}

export function GoalDetails(props: GoalDetailsProps) {
  const isExpanded = () => typeof props.expanded === 'function' ? props.expanded() : props.expanded;
  const goal = () => readSource(props.goal) || fallbackGoal();
  const decisions = () => readSource(props.decisions) || [];
  const width = () => Math.max(1, Number(readSource(props.width)) || 120);
  const height = () => Math.max(1, Number(readSource(props.height)) || 28);
  const tasks = () => taskRows(goal());
  const mode = () => layoutMode(width(), height());
  const short = () => mode() === 'short';
  const detailWidth = () => mode() === 'wide'
    ? Math.max(12, Math.floor(width() * 0.4) - 7)
    : Math.max(12, width() - 6);
  const activeDecision = () => [...decisions()].reverse().find(item => item.status === 'active')
    || [...decisions()].reverse()[0];
  const decisionRows = () => [...decisions()].slice(-6).reverse();
  const supervisor = () => {
    const current = goal();
    return (isSnapshot(current) ? current.supervision : undefined) as any;
  };
  const supervisorStatus = () => String(supervisor()?.status || '尚未启动');
  const contract = () => present(goal().goal_contract);
  const verification = () => present(goal().verification);
  const evidence = () => {
    const current = goal();
    return isSnapshot(current) ? present(current.final_verification?.stdout_tail) : undefined;
  };
  const error = () => {
    const goalError = present(goal().last_error);
    if (goalError) return goalError;
    return tasks().map(task => present(task.last_error)).find((value): value is string => value !== undefined);
  };

  const toggle = (source: string, event?: any) => {
    props.interactionTrace?.record({event: source === 'mouse' ? 'mouse_up' : 'key_down', target: 'GOAL_DETAILS_TOGGLE', bounds: eventCoordinates(event), detail: source === 'key' ? {key: event?.key} : undefined});
    props.interactionTrace?.record({event: 'callback_called', target: 'GOAL_DETAILS_TOGGLE', callback_called: true});
    props.onToggle();
  };
  const mouseDown = (event: any) => props.interactionTrace?.record({event: 'mouse_down', target: 'GOAL_DETAILS_TOGGLE', bounds: eventCoordinates(event)});
  const activateToggle = (event: any) => {
    const key = event?.key;
    if (key === 'Enter' || key === ' ' || key === 'Space' || key === 'Spacebar') toggle('key', event);
  };

  return (
    <box flexDirection="column" flexGrow={isExpanded() ? 1 : 0} flexShrink={isExpanded() ? 1 : 0} minHeight={0} minWidth={0} paddingX={1} paddingTop={mode() === 'wide' ? 1 : 0}>
      <box
        /* Keep the affordance content-sized.  A full-width, one-row Box can
         * overlap the preceding bordered summary on compact terminals (the
         * border glyphs then show through spaces in the label).  The parent
         * remains the click target while this row stays a plain, stable line. */
        flexShrink={0}
        height={1}
        onMouseDown={mouseDown}
        onMouseUp={(event: any) => toggle('mouse', event)}
        onKeyDown={activateToggle}
      >
        <text fg={C.secondary} selectable={false} content={`${isExpanded() ? '▾' : '▸'} ${short() ? '详情' : '详情面板'} · ${isExpanded() ? '任务图与证据' : '按 Enter 展开'}`} />
      </box>
      <Show when={isExpanded()} fallback={<box />}>
        <scrollbox flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
          <box flexDirection="column" minWidth={0} paddingTop={1} paddingBottom={1}>
            <text fg={C.secondary} wrapMode="none" truncate>TASK GRAPH · {tasks().length} 个任务</text>
            <For each={tasks()}>
              {(task, index) => <box flexDirection="row" minWidth={0}>
                <text fg={C.textMuted} wrapMode="none">{index() === tasks().length - 1 ? '┗' : '┣'} </text>
                <text fg={taskColor(task)} wrapMode="none">{taskIcon(task)} </text>
                <text fg={taskColor(task)} wrapMode="none" truncate flexGrow={1}>{clipTerminalText(task.subject, detailWidth())}</text>
                <Show when={!short()} fallback={<box />}><text fg={C.textMuted} wrapMode="none" truncate> · {task.status} · {task.verification_state}</text></Show>
              </box>}
            </For>
            <Show when={tasks().length === 0} fallback={<box />}><text fg={C.textMuted}>暂无任务详情</text></Show>

            <Show when={!short() && tasks().length > 0} fallback={<box />}>
              <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>TASK NOTES</text>
              <For each={tasks()}>
                {task => <box flexDirection="column" minWidth={0} marginBottom={1}>
                  <text fg={taskColor(task)} wrapMode="none" truncate>{taskIcon(task)} {task.subject}</text>
                  <For each={detailsForTask(task)}>
                    {line => <text fg={line.startsWith('Error') ? C.error : C.textMuted} wrapMode="word">  {line}</text>}
                  </For>
                </box>}
              </For>
            </Show>

            <Show when={!short() && decisionRows().length > 0} fallback={<box />}>
              <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>AGENTS · 最近动作</text>
              <For each={decisionRows()}>{decision => <box flexDirection="column" minWidth={0}>
                <text fg={decision.status === 'failed' ? C.error : decision.status === 'active' ? C.info : C.textMuted} wrapMode="none" truncate>
                  {clipTerminalText(`${decision.status === 'active' ? '●' : decision.status === 'failed' ? '×' : '✓'} ${decision.agent || 'Agent'}${decision.model ? ` · ${decision.model}` : ''}${decision.phase ? ` · ${decision.phase}` : ''}`, detailWidth())}
                </text>
                <Show when={decision.text} fallback={<box />}><text fg={C.textMuted} wrapMode="word" truncate>  {clipTerminalText(decision.text, detailWidth())}</text></Show>
              </box>}</For>
            </Show>

            <Show when={!short() && (supervisor() || activeDecision())} fallback={<box />}>
              <text fg={C.secondary} wrapMode="none" truncate marginTop={1}>SUPERVISOR · {supervisorStatus()}</text>
              <Show when={supervisor()?.latest?.summary || supervisor()?.latest?.next_step} fallback={<box />}>
                <text fg={supervisor()?.status === 'attention' ? C.warning : C.textMuted} wrapMode="word" truncate>
                  {supervisor()?.latest?.summary || supervisor()?.latest?.next_step}
                </text>
              </Show>
            </Show>

            <box border borderStyle="rounded" borderColor={C.info} flexDirection="column" minWidth={0} paddingX={1} marginTop={short() ? 1 : 0}>
              <text fg={C.secondary} wrapMode="none" truncate>MACHINE EVIDENCE</text>
              <Show when={contract()} fallback={<text fg={C.textMuted}>Contract：暂无</text>}><text fg={C.text} wrapMode="word">Contract：{contract()}</text></Show>
              <Show when={verification()} fallback={<text fg={C.textMuted}>Verification：暂无</text>}><text fg={C.text} wrapMode="word">Verification：{verification()}</text></Show>
              <Show when={tasks().some(task => task.evidence_count !== undefined)} fallback={<box />}><text fg={C.success}>Regression：执行链路与既有状态回归记录</text></Show>
              <Show when={evidence()} fallback={<box />}><text fg={C.success} wrapMode="word">Evidence：{evidence()}</text></Show>
              <Show when={error()} fallback={<box />}><text fg={C.error} wrapMode="word">Error：{error()}</text></Show>
            </box>
          </box>
        </scrollbox>
      </Show>
    </box>
  );
}

export default GoalDetails;
