import assert from 'node:assert/strict';
import {test} from 'node:test';
import {initialState, reduceEvent} from '../src/state.js';
import {buildTranscriptLines} from '../src/components/TranscriptBuffer.js';
import type {ChatItem, UiEvent} from '../src/types.js';

function feed(events: UiEvent[]): ReturnType<typeof reduceEvent> {
  return events.reduce((state, event) => reduceEvent(state, event), initialState);
}

test('subagent lifecycle groups into one scoped block', () => {
  const state = feed([
    {type: 'tool_start', id: 'main-1', name: 'task', summary: 'research X'},
    {type: 'subagent_start', id: 'a1', agent_type: 'explore', description: 'research X', model: 'mimo'},
    {type: 'subagent_round', id: 'a1', round: 1, text: 'looking'},
    {type: 'subagent_tool', id: 'a1', tool_use_id: 't1', name: 'glob', summary: 'src/**', ok: null},
    {type: 'subagent_tool', id: 'a1', tool_use_id: 't1', name: 'glob', summary: 'src/**', ok: true},
    {type: 'subagent_end', id: 'a1', ok: true, tools: 1, elapsed: 1.2, summary: 'found it'},
    {type: 'tool_end', id: 'main-1', name: 'task', ok: true, summary: 'found it'},
  ]);

  const items = state.items;
  const subagent = items.find((item): item is Extract<ChatItem, {kind: 'subagent'}> => item.kind === 'subagent');
  assert.ok(subagent, 'subagent block exists');
  assert.equal(subagent.agent.id, 'a1');
  assert.equal(subagent.agent.status, 'done');
  assert.equal(subagent.agent.toolCount, 1);
  assert.equal(subagent.agent.elapsed, 1.2);
  assert.equal(subagent.agent.tools.length, 1);
  assert.equal(subagent.agent.tools[0].status, 'done');
  assert.equal(subagent.agent.rounds.length, 1);
});

test('subagent running state keeps nested tools expanding', () => {
  const state = feed([
    {type: 'subagent_start', id: 'b1', agent_type: 'explore', description: 'scan', model: 'mimo'},
    {type: 'subagent_tool', id: 'b1', tool_use_id: 't1', name: 'read_file', summary: 'a.md', ok: null},
  ]);
  const agent = state.items.find((item): item is Extract<ChatItem, {kind: 'subagent'}> => item.kind === 'subagent')!.agent;
  assert.equal(agent.status, 'running');
  assert.equal(agent.tools[0].status, 'running');
});

test('transcript renders expanded running block and collapsed done line', () => {
  const runningState = feed([
    {type: 'subagent_start', id: 'c1', agent_type: 'explore', description: 'scan', model: 'mimo'},
    {type: 'subagent_tool', id: 'c1', tool_use_id: 't1', name: 'glob', summary: 'src/**', ok: null},
  ]);
  const runningLines = buildTranscriptLines(runningState.items, 80).map(l => l.text);
  assert.ok(runningLines.some(l => l.includes('⠙ explore') && l.includes('scan')), 'header visible while running');
  assert.ok(runningLines.some(l => l.includes('glob')), 'nested tool visible while running');

  const doneState = feed([
    {type: 'subagent_start', id: 'c2', agent_type: 'explore', description: 'scan', model: 'mimo'},
    {type: 'subagent_tool', id: 'c2', tool_use_id: 't1', name: 'glob', summary: 'src/**', ok: null},
    {type: 'subagent_tool', id: 'c2', tool_use_id: 't1', name: 'glob', summary: 'src/**', ok: true},
    {type: 'subagent_end', id: 'c2', ok: true, tools: 1, elapsed: 0.4, summary: 'ok'},
  ]);
  const doneLines = buildTranscriptLines(doneState.items, 80).map(l => l.text);
  const doneRow = doneLines.find(l => l.includes('✓ explore'));
  assert.ok(doneRow, 'collapsed done line present');
  assert.ok(doneRow!.includes('1 tools') && doneRow!.includes('0.4s'), 'stats in collapsed line');
  // nested tool lines are gone after collapse
  assert.ok(!doneLines.some(l => l.includes('⠙ glob')), 'nested tools collapsed');
});

test('failed subagent renders failure mark', () => {
  const state = feed([
    {type: 'subagent_start', id: 'd1', agent_type: 'explore', description: 'boom', model: 'mimo'},
    {type: 'subagent_end', id: 'd1', ok: false, tools: 0, elapsed: 0.1, summary: ''},
  ]);
  const lines = buildTranscriptLines(state.items, 80).map(l => l.text);
  assert.ok(lines.some(l => l.includes('✕ explore')), 'failure mark visible');
});
