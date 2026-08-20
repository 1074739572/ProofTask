import assert from 'node:assert/strict';
import test from 'node:test';
import {
  goalBlocksChat,
  goalDraftEventShouldFocus,
  goalDraftHasQuestion,
  goalDraftIsBusy,
  goalDraftSnapshotFromEvent,
  goalEventShouldFocus,
  goalNextActionPresentation,
  goalSnapshotFromEvent,
  mergeGoalDiscoveryEvent,
  type GoalDraftSnapshot,
  type GoalSnapshot,
} from '../src-open/GoalView.tsx';

function draft(overrides: Partial<GoalDraftSnapshot> = {}): GoalDraftSnapshot {
  return {
    id: 'draft-1', target: 'add rate limits', status: 'clarifying', stage: 'intake',
    intake_assumptions: [], clarifications: [], question: '', question_index: 0,
    question_count: 0, task_count: 0, tasks: [], discovery_jobs: [], ...overrides,
  };
}

function goal(overrides: Partial<GoalSnapshot> = {}): GoalSnapshot {
  return {id: 'goal-1', target: 'add rate limits', phase: 'act', status: 'running', tasks: [], ...overrides};
}

test('empty intake keeps visible progress while discovery and planning continue', () => {
  const snapshot = goalDraftSnapshotFromEvent({
    seq: 4, ts: 4, id: 'draft-1', target: 'add rate limits', status: 'clarifying', stage: 'intake',
    intake_summary: '需求清晰，可以继续', intake_assumptions: ['保留现有用户身份'], question: '',
  });
  assert(snapshot);
  assert.equal(goalDraftHasQuestion(snapshot), false);
  assert.equal(goalDraftIsBusy(snapshot), true);
});

test('a real clarification checkpoint accepts an answer and is not busy', () => {
  const snapshot = draft({question: '按用户还是按 API key 限流？', question_count: 1});
  assert.equal(goalDraftHasQuestion(snapshot), true);
  assert.equal(goalDraftIsBusy(snapshot), false);
});

test('startup hydration keeps inactive Goal and Draft snapshots out of normal chat', () => {
  const pausedGoal = goal({status: 'paused', phase: 'paused'});
  const waitingDraft = draft({status: 'clarifying', question: 'Which scope?', question_count: 1});
  const staleBusyDraft = draft({status: 'discovering', stage: 'discovering'});

  assert.equal(goalEventShouldFocus({type: 'goal_status', hydrated: true}, pausedGoal), false);
  assert.equal(goalDraftEventShouldFocus({type: 'goal_draft_status', event: 'hydrated'}, waitingDraft, pausedGoal), false);
  assert.equal(goalDraftEventShouldFocus({type: 'goal_draft_status', event: 'hydrated'}, staleBusyDraft, null), false);
  assert.equal(goalBlocksChat(pausedGoal, true), false);
});

test('active and explicitly requested Goal events focus their workflow pages', () => {
  const activeGoal = goal();
  const pausedGoal = goal({status: 'paused', phase: 'paused'});
  const waitingDraft = draft({status: 'ready', stage: 'ready'});

  assert.equal(goalEventShouldFocus({type: 'goal_status', hydrated: true}, activeGoal), true);
  assert.equal(goalEventShouldFocus({type: 'goal_status'}, pausedGoal), true);
  assert.equal(goalDraftEventShouldFocus({type: 'goal_draft_status', event: 'status'}, waitingDraft, pausedGoal), true);
  assert.equal(goalBlocksChat(activeGoal, true), true);
});

test('discovery events update one visible job instead of duplicating it', () => {
  const started = goalDraftSnapshotFromEvent({seq: 1, ts: 1, id: 'draft-1', target: 'x', status: 'discovering', stage: 'discovering'});
  assert(started);
  const withJob = mergeGoalDiscoveryEvent(started, {
    seq: 2, ts: 2, goal_id: 'draft-1', event: 'started', job_id: 'implementation-1', role: 'implementation',
    read_path_count: 4, read_paths: ['src/a.py'], tools: ['read_file'],
  });
  const completed = mergeGoalDiscoveryEvent(withJob, {
    seq: 3, ts: 3, goal_id: 'draft-1', event: 'completed', job_id: 'implementation-1', role: 'implementation',
  });
  assert.equal(completed.discovery_jobs.length, 1);
  assert.equal(completed.discovery_jobs[0].status, 'done');
  assert.equal(completed.discovery_jobs[0].read_path_count, 4);
});

test('older goal snapshots cannot replace a newer durable phase', () => {
  const current = goal({phase: 'verify', status: 'running', updated_at: 20, event_seq: 20});
  const older = goalSnapshotFromEvent({id: 'goal-1', target: 'add rate limits', phase: 'act', status: 'running', updated_at: 19, seq: 19}, current);
  assert.equal(older?.phase, 'verify');
});

test('paused stop reasons expose actionable commands', () => {
  assert.equal(goalNextActionPresentation(goal({status: 'paused', stop_reason: 'user_approval_required'})).command, '/goal run');
  assert.match(goalNextActionPresentation(goal({status: 'paused', stop_reason: 'permission_wait'})).command, /resume/);
  assert.equal(goalNextActionPresentation(goal({status: 'paused', stop_reason: 'task_failed'})).command, '/goal resume');
  assert.equal(goalNextActionPresentation(goal({status: 'done'})).command, '开始新的 /goal');
});
