import {For, Show} from 'solid-js';
import {C} from './theme.ts';

export type GoalAcceptanceCase = {id?: string; given?: string; when?: string; then?: string};
export type GoalEvidence = {
  command?: string;
  exit_code?: number | null;
  stdout_tail?: string;
  duration_ms?: number | null;
  verified_by?: string;
  code_snapshot?: string;
  selectors?: string[];
  collected_count?: number;
};
export type GoalVerificationSpec = {
  command?: string;
  selectors?: string[];
  source?: string;
  baseline_result?: string;
  collected_count?: number;
};
export type GoalFinalVerification = GoalEvidence & {
  status?: string;
  error?: string;
  updated_at?: number;
};
export type GoalTaskSnapshot = {
  id: string;
  subject: string;
  status: string;
  verification_state: string;
  blocked_by?: string[];
  acceptance_cases?: GoalAcceptanceCase[];
  verification_spec?: GoalVerificationSpec;
  evidence_count?: number;
  latest_evidence?: GoalEvidence | null;
  last_error?: string | null;
};

export type GoalSnapshot = {
  id: string;
  target: string;
  verification?: string;
  phase: string;
  status: string;
  current_task_id?: string | null;
  attempts?: number;
  max_attempts?: number;
  total_llm_rounds?: number;
  max_total_rounds?: number;
  stop_reason?: string | null;
  final_verification?: GoalFinalVerification | null;
  tasks: GoalTaskSnapshot[];
  last_error?: string | null;
};

export type GoalTone = 'success' | 'warning' | 'error' | 'info' | 'muted';
export type GoalPresentation = {tone: GoalTone; icon: string; text: string};

const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled']);

const PHASE_LABELS: Record<string, string> = {
  initialize: '生成任务契约',
  prepare_tests: '准备验收测试',
  select_task: '选择下一任务',
  claim: '接管任务',
  act: '实现当前任务',
  verify: '运行绑定测试',
  evaluate: '独立质量评审',
  clean_check: '执行清洁检查',
  full_verify: '最终全局回归',
  done: '交付完成',
  paused: '已暂停',
  cancelling: '正在取消',
  cancelled: '已取消',
  failed: '执行失败',
};

function toneColor(tone: GoalTone): string {
  if (tone === 'success') return C.success;
  if (tone === 'warning') return C.warning;
  if (tone === 'error') return C.error;
  if (tone === 'info') return C.info;
  return C.textMuted;
}

function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] || phase.replaceAll('_', ' ');
}

function verificationLabel(state: string): string {
  if (state === 'passing') return '已通过';
  if (state === 'failing') return '未通过';
  if (state === 'needs_generation') return '等待生成测试';
  if (state === 'not_started') return '尚未验证';
  if (state === 'unknown') return '状态未知';
  return state.replaceAll('_', ' ');
}

export function goalStatusPresentation(status: string): GoalPresentation {
  if (status === 'done') return {tone: 'success', icon: '✓', text: '已交付'};
  if (status === 'failed') return {tone: 'error', icon: '×', text: '失败'};
  if (status === 'cancelled') return {tone: 'muted', icon: '■', text: '已取消'};
  if (status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: '已暂停'};
  if (status === 'pausing') return {tone: 'warning', icon: 'Ⅱ', text: '正在暂停'};
  if (status === 'cancelling') return {tone: 'warning', icon: '■', text: '正在取消'};
  return {tone: 'info', icon: '●', text: '执行中'};
}

export function baselinePresentation(result?: string, source?: string): GoalPresentation {
  if (source === 'needs_generation') return {tone: 'warning', icon: '○', text: '失败基线：等待生成测试'};
  if (result === 'failing') return {tone: 'success', icon: '✓', text: '失败基线：已确认（实现前测试失败）'};
  if (source && source !== 'generated') return {tone: 'muted', icon: '·', text: '失败基线：不适用（复用已有测试）'};
  return {tone: 'warning', icon: '○', text: '失败基线：尚未确认'};
}

function taskPresentation(task: GoalTaskSnapshot, current: boolean, tasks: GoalTaskSnapshot[]): GoalPresentation {
  if (task.status === 'missing') return {tone: 'error', icon: '!', text: '状态缺失'};
  if (task.status === 'completed') return {tone: 'success', icon: '✓', text: '已完成'};
  if (current && task.verification_state === 'failing') return {tone: 'error', icon: '×', text: '测试失败，修复中'};
  if (current) return {tone: 'warning', icon: '●', text: '当前任务'};
  const incomplete = (task.blocked_by || []).filter(id => tasks.find(item => item.id === id)?.status !== 'completed');
  if (incomplete.length) return {tone: 'muted', icon: '○', text: `等待 ${incomplete.length} 个前置任务`};
  return {tone: 'muted', icon: '○', text: '等待执行'};
}

export function gatePresentation(goal: GoalSnapshot, task?: GoalTaskSnapshot): GoalPresentation {
  if (goal.status === 'done') return {tone: 'success', icon: '✓', text: '全部 Task 证据与最终回归均已通过，可以交付'};
  if (goal.status === 'failed') return {tone: 'error', icon: '×', text: '机器门禁未通过，Goal 已停止'};
  if (goal.status === 'cancelled') return {tone: 'muted', icon: '■', text: 'Goal 已取消，现有证据保留'};
  if (goal.status === 'paused') return {tone: 'warning', icon: 'Ⅱ', text: 'Goal 已暂停，可以恢复后继续验证'};
  if (goal.status === 'pausing') return {tone: 'warning', icon: 'Ⅱ', text: '正在等待当前操作到达暂停点'};
  if (goal.status === 'cancelling') return {tone: 'warning', icon: '■', text: '正在等待当前操作安全停止'};
  if (!task) return {tone: 'info', icon: '●', text: '正在生成可验证的 Task 契约'};
  if (goal.phase === 'prepare_tests') return {tone: 'warning', icon: '○', text: '等待测试生成、收集和失败基线确认'};
  if (goal.phase === 'verify') return {tone: 'warning', icon: '●', text: '正在等待绑定测试的机器结果'};
  if (goal.phase === 'evaluate') return {tone: 'info', icon: '●', text: '测试已通过，正在进行独立质量评审'};
  if (goal.phase === 'clean_check') return {tone: 'warning', icon: '●', text: '测试已通过，正在执行严格 clean check'};
  if (goal.phase === 'full_verify') return {tone: 'warning', icon: '●', text: '正在重跑全部 Task 测试和全局回归'};
  if (task.verification_state === 'failing') return {tone: 'error', icon: '×', text: '上一次机器测试失败，模型正在修复当前 Task'};
  if (task.verification_state === 'passing') return {tone: 'info', icon: '✓', text: '绑定测试已通过，仍需完成后续交付门禁'};
  return {tone: 'warning', icon: '○', text: 'Task 尚未取得零退出码测试证据'};
}

function sourceLabel(source?: string): string {
  if (source === 'generated') return '生成测试';
  if (source === 'discovered') return '已有测试目录';
  if (source === 'user') return '用户指定';
  if (source === 'needs_generation') return '等待生成';
  return source || '未知来源';
}

function formatDuration(duration?: number | null): string {
  if (duration == null || !Number.isFinite(duration)) return '-';
  if (duration < 1000) return `${Math.round(duration)} ms`;
  return `${(duration / 1000).toFixed(1)} s`;
}

function evidenceLines(value?: string, limit = 5): string[] {
  const clean = String(value || '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
  return clean.split(/\r?\n/).map(line => line.trimEnd()).filter(Boolean).slice(-limit);
}

function progressBar(completed: number, total: number, width: number): string {
  const filled = total > 0 ? Math.round((completed / total) * width) : 0;
  return `${'█'.repeat(filled)}${'░'.repeat(Math.max(0, width - filled))}`;
}

export function goalRegressionPresentation(goal: GoalSnapshot): GoalPresentation {
  const final = goal.final_verification;
  if (final?.status === 'passed' && final.exit_code === 0) return {tone: 'success', icon: '✓', text: `通过 · exit 0 · ${formatDuration(final.duration_ms)}`};
  if (final?.status === 'failed') return {tone: 'error', icon: '×', text: `失败 · 退出码 ${final.exit_code ?? '-'}`};
  if (final?.status === 'blocked') return {tone: 'error', icon: '×', text: '未运行：Task 最终绑定未通过'};
  if (final?.status === 'running') return {tone: 'warning', icon: '●', text: '正在运行'};
  if (goal.status === 'done') return {tone: 'success', icon: '✓', text: '状态机结果：通过'};
  if (goal.status === 'failed' && goal.stop_reason === 'full_verification_failed') return {tone: 'error', icon: '×', text: '状态机结果：失败'};
  if (goal.phase === 'full_verify') return {tone: 'warning', icon: '●', text: '状态机结果：运行中'};
  if (goal.status === 'cancelled') return {tone: 'muted', icon: '■', text: '状态机结果：未完成'};
  return {tone: 'muted', icon: '○', text: '状态机结果：等待全部 Task 完成'};
}

export function GoalView(props: {goal: GoalSnapshot; width: number; height: number}) {
  const compact = () => props.width < 76;
  const current = () => props.goal.tasks.find(task => task.id === props.goal.current_task_id);
  const completed = () => props.goal.tasks.filter(task => task.status === 'completed').length;
  const status = () => goalStatusPresentation(props.goal.status);
  const gate = () => gatePresentation(props.goal, current());
  const latest = () => current()?.latest_evidence;
  const output = () => evidenceLines(latest()?.stdout_tail, compact() ? 3 : 5);
  const regression = () => goalRegressionPresentation(props.goal);
  const finalOutput = () => evidenceLines(props.goal.final_verification?.stdout_tail, compact() ? 2 : 3);
  const terminal = () => TERMINAL_STATUSES.has(props.goal.status);
  const gatePanel = () => <box border borderStyle="rounded" borderColor={toneColor(gate().tone)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
    <text fg={toneColor(gate().tone)}>机器门禁</text>
    <text fg={C.text} wrapMode="word">{gate().icon} {gate().text}</text>
    <Show when={props.goal.last_error || current()?.last_error}>
      <text fg={C.error} wrapMode="word">最近错误：{props.goal.last_error || current()?.last_error}</text>
    </Show>
    <Show when={props.goal.stop_reason}>
      <text fg={C.textMuted} wrapMode="word">停止原因：{props.goal.stop_reason}</text>
    </Show>
  </box>;
  const regressionPanel = () => <box border borderStyle="rounded" borderColor={toneColor(regression().tone)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
    <text fg={C.secondary}>Goal 最终回归</text>
    <text fg={C.info} wrapMode="word">{props.goal.verification || '未配置全局回归命令'}</text>
    <text fg={toneColor(regression().tone)}>{regression().icon} {regression().text}</text>
    <Show when={props.goal.final_verification?.code_snapshot}>
      <text fg={C.textMuted} wrapMode="none" truncate>代码快照 {props.goal.final_verification?.code_snapshot}</text>
    </Show>
    <For each={finalOutput()}>{line => <text fg={C.textMuted} wrapMode="word">  {line}</text>}</For>
    <Show when={props.goal.final_verification?.error}>
      <text fg={C.error} wrapMode="word">{props.goal.final_verification?.error}</text>
    </Show>
  </box>;

  return <scrollbox height={props.height} flexShrink={0} stickyScroll viewportOptions={{paddingRight: 1}} verticalScrollbarOptions={{visible: true}}>
    <box flexDirection="column" paddingX={1} paddingTop={1} paddingBottom={1}>
      <box flexDirection={compact() ? 'column' : 'row'} justifyContent="space-between" minWidth={0}>
        <text fg={toneColor(status().tone)}>{status().icon} GOAL · {status().text}</text>
        <text fg={C.warning}>{phaseLabel(props.goal.phase)}</text>
      </box>
      <text fg={C.text} wrapMode="word">{props.goal.target}</text>
      <box flexDirection="row" minWidth={0} gap={1}>
        <text fg={C.success} wrapMode="none">{progressBar(completed(), props.goal.tasks.length, compact() ? 10 : 18)}</text>
        <text fg={C.textMuted} wrapMode="none">{completed()}/{props.goal.tasks.length} Tasks</text>
        <Show when={(props.goal.max_total_rounds || 0) > 0}>
          <text fg={C.textMuted} wrapMode="none">· {props.goal.total_llm_rounds || 0}/{props.goal.max_total_rounds} rounds</text>
        </Show>
      </box>
      <text fg={C.textMuted} wrapMode="none" truncate>{props.goal.id}</text>

      <box border borderStyle="rounded" borderColor={C.textMuted} paddingX={1} flexDirection="column" minWidth={0}>
        <text fg={C.secondary}>任务图</text>
        <For each={props.goal.tasks}>{(task, index) => {
          const active = () => task.id === props.goal.current_task_id && !terminal();
          const presentation = () => taskPresentation(task, active(), props.goal.tasks);
          return <box flexDirection={compact() ? 'column' : 'row'} minWidth={0}>
            <box flexDirection="row" minWidth={0} flexGrow={1}>
              <text fg={toneColor(presentation().tone)} wrapMode="none">{presentation().icon} {String(index() + 1).padStart(2, '0')} </text>
              <text fg={active() ? C.text : C.textMuted} wrapMode="none" truncate flexGrow={1}>{task.subject}</text>
            </box>
            <text fg={toneColor(presentation().tone)} wrapMode="none">{compact() ? '    ' : ' · '}{presentation().text}</text>
          </box>;
        }}</For>
      </box>

      <Show when={current()}>
        {task => <>
          <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
            <box border borderStyle="rounded" borderColor={terminal() ? C.textMuted : C.warning} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={terminal() ? C.secondary : C.warning}>{terminal() ? '最后 Task' : '当前 Task'} · {task().subject}</text>
              <text fg={C.textMuted} wrapMode="word">尝试 {props.goal.attempts || 0}/{props.goal.max_attempts || 0} · {verificationLabel(task().verification_state)}</text>
              <Show when={(task().acceptance_cases || []).length}>
                <text fg={C.secondary}>验收条件</text>
                <For each={task().acceptance_cases || []}>{(item, index) =>
                  <text fg={C.text} wrapMode="word">  {item.id || `AC${index() + 1}`} · 给定 {item.given || '-'} · 当 {item.when || '-'} · 则 {item.then || '-'}</text>
                }</For>
              </Show>
            </box>

            <box border borderStyle="rounded" borderColor={C.textMuted} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={C.secondary}>测试绑定</text>
              <text fg={task().verification_spec?.command ? C.info : C.warning} wrapMode="word">{task().verification_spec?.command || '测试尚未绑定'}</text>
              <Show when={(task().verification_spec?.selectors || []).length}>
                <For each={task().verification_spec?.selectors || []}>{selector => <text fg={C.textMuted} wrapMode="word">  ↳ {selector}</text>}</For>
              </Show>
              <text fg={C.textMuted}>来源 {sourceLabel(task().verification_spec?.source)} · 收集 {task().verification_spec?.collected_count || 0} 项</text>
              {(() => {
                const baseline = baselinePresentation(task().verification_spec?.baseline_result, task().verification_spec?.source);
                return <text fg={toneColor(baseline.tone)}>{baseline.icon} {baseline.text}</text>;
              })()}
            </box>
          </box>

          <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
            <box border borderStyle="rounded" borderColor={latest()?.exit_code === 0 ? C.success : (latest() ? C.error : C.textMuted)} paddingX={1} flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1}>
              <text fg={C.secondary}>最近一次机器证据</text>
              <Show when={latest()} fallback={<text fg={C.textMuted}>○ 尚未产生测试运行证据</text>}>
                {evidence => <>
                  <text fg={evidence().exit_code === 0 ? C.success : C.error}>
                    {evidence().exit_code === 0 ? '✓ 通过' : `× 退出码 ${evidence().exit_code ?? '-'}`} · {formatDuration(evidence().duration_ms)} · 收集 {evidence().collected_count || 0} 项
                  </text>
                  <Show when={evidence().code_snapshot}>
                    <text fg={C.textMuted} wrapMode="none" truncate>代码快照 {evidence().code_snapshot}</text>
                  </Show>
                  <For each={output()}>{line => <text fg={C.textMuted} wrapMode="word">  {line}</text>}</For>
                  <text fg={C.textMuted}>累计保存 {task().evidence_count || 0} 次验证结果</text>
                </>}
              </Show>
            </box>
            <box flexDirection="column" minWidth={0} flexGrow={compact() ? 0 : 1} gap={1}>
              {gatePanel()}
              {regressionPanel()}
            </box>
          </box>
        </>}
      </Show>

      <Show when={!current()}>
        <box flexDirection={compact() ? 'column' : 'row'} minWidth={0} gap={1}>
          {gatePanel()}
          {regressionPanel()}
        </box>
      </Show>
    </box>
  </scrollbox>;
}
