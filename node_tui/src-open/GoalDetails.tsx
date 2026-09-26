import {For, Show} from 'solid-js';
import type {GoalDraftSnapshot, GoalTaskSnapshot} from './goal-state.ts';
import {
  GOAL_SIDE_COLUMN_RATIO,
  fallbackGoal,
  goalTaskColor,
  goalTaskIcon,
  goalTaskState,
  isSnapshot,
  readSource,
  type GoalLike,
} from './goal-presentation.ts';
import {eventCoordinates, type InteractionTrace} from './interaction-trace.ts';
import {C} from './theme.ts';
import {clipTerminalText, layoutMode} from './layout.ts';

type GoalSource = GoalLike | (() => GoalLike);

export type GoalDetailsProps = {
  goal: GoalSource;
  expanded: boolean | (() => boolean);
  onToggle: () => void;
  interactionTrace?: InteractionTrace;
  width?: number | (() => number);
  height?: number | (() => number);
};

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
    detailLine('契约命令', task.verification_spec?.command),
    detailLine('验证策略', task.test_strategy),
    detailLine('证据输出', task.latest_evidence?.stdout_tail),
    detailLine('回归证据', task.evidence_count === undefined ? undefined : `${task.evidence_count} 条证据`),
    detailLine('错误', task.last_error),
  ];
  return lines.filter((line): line is string => typeof line === 'string' && line.length > 0);
}

export function GoalDetails(props: GoalDetailsProps) {
  const isExpanded = () => typeof props.expanded === 'function' ? props.expanded() : props.expanded;
  const goal = () => readSource(props.goal) || fallbackGoal();
  const width = () => Math.max(1, Number(readSource(props.width)) || 120);
  const height = () => Math.max(1, Number(readSource(props.height)) || 28);
  const tasks = () => taskRows(goal());
  const mode = () => layoutMode(width(), height());
  const short = () => mode() === 'short';
  // 宽屏下面板位于右栏，可用宽度与执行流一致（GOAL_SIDE_COLUMN_RATIO）。
  const detailWidth = () => mode() === 'wide'
    ? Math.max(12, Math.floor(width() * GOAL_SIDE_COLUMN_RATIO) - 7)
    : Math.max(12, width() - 6);
  const currentTaskId = () => {
    const current = goal();
    return isSnapshot(current) ? current.current_task_id : undefined;
  };
  const stateOf = (task: GoalTaskSnapshot) => goalTaskState(task, currentTaskId());
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
        <text fg={C.secondary} selectable={false} content={`${isExpanded() ? '▾' : '▸'} ${short() ? '详情' : '详情面板'} · ${isExpanded() ? '任务明细与证据' : '按 Enter 展开'}`} />
      </box>
      <Show when={isExpanded()} fallback={<box />}>
        <scrollbox flexGrow={1} flexShrink={1} minHeight={0} minWidth={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
          <box flexDirection="column" minWidth={0} paddingTop={1} paddingBottom={1}>
            <Show when={tasks().length > 0} fallback={<text fg={C.textMuted}>暂无任务详情</text>}>
              <text fg={C.secondary} wrapMode="none" truncate>任务明细 · {tasks().length} 个任务</text>
              <For each={tasks()}>
                {task => <box flexDirection="column" minWidth={0} marginBottom={1}>
                  <text fg={goalTaskColor(stateOf(task))} wrapMode="none" truncate>{goalTaskIcon(stateOf(task))} {clipTerminalText(task.subject, detailWidth())}</text>
                  <For each={detailsForTask(task)}>
                    {line => <text fg={line.startsWith('错误') ? C.error : C.textMuted} wrapMode="word">  {line}</text>}
                  </For>
                </box>}
              </For>
            </Show>

            <box border borderStyle="rounded" borderColor={C.info} flexDirection="column" minWidth={0} paddingX={1} marginTop={short() ? 1 : 0}>
              <text fg={C.secondary} wrapMode="none" truncate>机器证据</text>
              <Show when={contract()} fallback={<text fg={C.textMuted}>契约：暂无</text>}><text fg={C.text} wrapMode="word">契约：{contract()}</text></Show>
              <Show when={verification()} fallback={<text fg={C.textMuted}>验证：暂无</text>}><text fg={C.text} wrapMode="word">验证：{verification()}</text></Show>
              <Show when={tasks().some(task => task.evidence_count !== undefined)} fallback={<box />}><text fg={C.success}>回归：执行链路与既有状态回归记录</text></Show>
              <Show when={evidence()} fallback={<box />}><text fg={C.success} wrapMode="word">证据：{evidence()}</text></Show>
              <Show when={error()} fallback={<box />}><text fg={C.error} wrapMode="word">错误：{error()}</text></Show>
            </box>
          </box>
        </scrollbox>
      </Show>
    </box>
  );
}

export default GoalDetails;
