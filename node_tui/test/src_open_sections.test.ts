import assert from 'node:assert/strict';
import {test} from 'node:test';

import {buildSections} from '../src-open/sections.ts';

const todo = (id: string, status: 'pending' | 'in_progress' | 'completed') => ({id, content: id, status, activeForm: id});

test('turn summary lands before the final response segment (chronological order)', () => {
  // Real event flow: interim text → tools → final answer → agent_end summary.
  const sections = buildSections([
    {id: 'p1', kind: 'prompt', text: 'fix the 500'},
    {id: 'r1', kind: 'response', text: '先看日志'},
    {id: 'a1', kind: 'action', text: 'bash', detail: 'grep 500', done: true, ok: true},
    {id: 'r2', kind: 'response', text: '问题已修复'},
    {id: 'sum1', kind: 'summary', text: 'Turn complete', toolCount: 1, paths: [], tokens: {inp: 0, out: 0, cache: 0}, expanded: false},
  ]);

  assert.deepEqual(sections.map(section => section.kind), ['prompt', 'response', 'summary', 'response']);
  assert.equal(sections[1].kind === 'response' && sections[1].text, '先看日志');
  assert.equal(sections[3].kind === 'response' && sections[3].text, '问题已修复');
});

test('merged same-name tool calls unfold into individual numbered steps', () => {
  const sections = buildSections([
    {id: 'p1', kind: 'prompt', text: 'run tests'},
    {id: 'i1', kind: 'intent', text: '先跑一遍测试'},
    {id: 'a1', kind: 'action', text: 'bash', detail: 'pytest -x', done: true, ok: true},
    {id: 'a2', kind: 'action', text: 'bash', detail: 'pytest tests/e2e', done: true, ok: false},
    {id: 'sum1', kind: 'summary', text: 'Turn complete', toolCount: 2, paths: [], tokens: {inp: 0, out: 0, cache: 0}, expanded: true},
  ]);

  const summary = sections.find((section: any) => section.kind === 'summary') as any;
  assert.ok(summary, 'summary exists');
  assert.deepEqual(summary.steps.map((step: any) => step.type), ['intent', 'tool', 'tool']);
  // Each call keeps its own summary/result instead of collapsing into
  // "bash · Called 2 times".
  assert.equal(summary.steps[1].row.summary, 'pytest -x');
  assert.equal(summary.steps[1].row.ok, true);
  assert.equal(summary.steps[2].row.summary, 'pytest tests/e2e');
  assert.equal(summary.steps[2].row.ok, false);
});

test('live actions view still collapses consecutive same-name calls', () => {
  const sections = buildSections([
    {id: 'a1', kind: 'action', text: 'bash', detail: 'cmd1', done: true, ok: true},
    {id: 'a2', kind: 'action', text: 'bash', detail: 'cmd2', done: true, ok: true},
  ]);

  assert.equal(sections.length, 1);
  assert.equal(sections[0].kind, 'actions');
  const rows = (sections[0] as any).rows;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 2);
  assert.equal(rows[0].calls.length, 2);
});

test('plan panel is pinned to the bottom and empty plans render nothing', () => {
  const sections = buildSections([
    {id: 'tasks:current', kind: 'tasks', text: '计划', tasks: [todo('a', 'in_progress'), todo('b', 'pending')]},
    {id: 'p1', kind: 'prompt', text: 'do the thing'},
    {id: 'r1', kind: 'response', text: 'done'},
  ]);

  assert.equal(sections[sections.length - 1].kind, 'tasks');
  assert.equal(sections[0].kind, 'prompt');

  const empty = buildSections([
    {id: 'tasks:current', kind: 'tasks', text: '计划', tasks: []},
    {id: 'p1', kind: 'prompt', text: 'hi'},
  ]);
  assert.equal(empty.some((section: any) => section.kind === 'tasks'), false);
});
