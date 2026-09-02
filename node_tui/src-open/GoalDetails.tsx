import {For, Show} from 'solid-js';
import type {GoalDraftSnapshot, GoalSnapshot, GoalTaskSnapshot} from './goal-state.ts';
import {eventCoordinates, type InteractionTrace} from './interaction-trace.ts';
import {C} from './theme.ts';

type GoalLike = GoalSnapshot | GoalDraftSnapshot;

export type GoalDetailsProps = {
  goal: GoalLike;
  expanded: boolean | (() => boolean);
  onToggle: () => void;
  interactionTrace?: InteractionTrace;
};

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
  const tasks = () => taskRows(props.goal);
  const contract = () => present(props.goal.goal_contract);
  const verification = () => present(props.goal.verification);
  const evidence = () => isSnapshot(props.goal) ? present(props.goal.final_verification?.stdout_tail) : undefined;
  const error = () => {
    const goalError = present(props.goal.last_error);
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
    <box flexDirection="column" flexGrow={1} flexShrink={0} minWidth={0} paddingX={1}>
      <box
        width="100%"
        height={1}
        onMouseDown={mouseDown}
        onMouseUp={(event: any) => toggle('mouse', event)}
        onKeyDown={activateToggle}
      >
        <text fg={C.secondary} selectable={false} content={`${isExpanded() ? '▾' : '▸'} 详情面板 · ${isExpanded() ? '任务图与证据' : '按 Enter 展开'} · GOAL_DETAILS_TOGGLE`} />
      </box>
      <Show when={isExpanded()} fallback={<box />}>
        <box flexDirection="column" marginTop={1} minWidth={0}>
          <box border borderStyle="rounded" borderColor={C.textMuted} flexDirection="column" minWidth={0} paddingX={1}>
            <text fg={C.secondary}>TASK GRAPH · {tasks().length} 个任务</text>
            <For each={tasks()}>
              {(task, index) => <box flexDirection="row" minWidth={0}>
                <text fg={C.textMuted} wrapMode="none">{index() === tasks().length - 1 ? '┗' : '┣'} </text>
                <text fg={taskColor(task)} wrapMode="none">{taskIcon(task)} </text>
                <text fg={taskColor(task)} wrapMode="none" truncate>{task.subject}</text>
                <text fg={C.textMuted} wrapMode="none" truncate> · {task.status} · {task.verification_state}</text>
              </box>}
            </For>
          </box>
          <Show when={tasks().length > 0} fallback={<box />}>
            <box flexDirection="column" minWidth={0} marginTop={1}>
              <For each={tasks()}>
                {task => (
                <box border borderStyle="rounded" borderColor={taskColor(task)} flexDirection="column" minWidth={0} paddingX={1} marginBottom={1}>
                  <text fg={taskColor(task)} wrapMode="none" truncate>{taskIcon(task)} {task.subject}</text>
                  <For each={detailsForTask(task)}>
                    {line => <text fg={line.startsWith('Error') ? C.error : C.textMuted} wrapMode="word">{line}</text>}
                  </For>
                </box>
                )}
              </For>
            </box>
          </Show>
          <box border borderStyle="rounded" borderColor={C.info} flexDirection="column" minWidth={0} paddingX={1}>
            <text fg={C.secondary}>MACHINE EVIDENCE</text>
            <Show when={contract()} fallback={<text fg={C.textMuted}>Contract：暂无</text>}><text fg={C.text} wrapMode="word">Contract：{contract()}</text></Show>
            <Show when={verification()} fallback={<text fg={C.textMuted}>Verification：暂无</text>}><text fg={C.text} wrapMode="word">Verification：{verification()}</text></Show>
            <Show when={tasks().some(task => task.evidence_count !== undefined)}><text fg={C.success}>Regression：执行链路与既有状态回归记录</text></Show>
            <Show when={evidence()}><text fg={C.success} wrapMode="word">Evidence：{evidence()}</text></Show>
            <Show when={error()}><text fg={C.error} wrapMode="word">Error：{error()}</text></Show>
          </box>
        </box>
      </Show>
    </box>
  );
}

export default GoalDetails;
