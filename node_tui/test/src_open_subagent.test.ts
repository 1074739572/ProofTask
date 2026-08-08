import assert from 'node:assert/strict';
import {test} from 'node:test';

import {buildSections} from '../src-open/sections.ts';

const todo = (id: string, status: 'pending' | 'in_progress' | 'completed') => ({id, content: id, status, activeForm: id});

test('src-open keeps running subagent in a bordered section', () => {
  const sections = buildSections([
    {id: 's1', kind: 'subagent', text: 'scan ui path', agentType: 'explore', model: 'mimo', status: 'running', rounds: ['Round 1 · "looking"'], tools: [{id: 't1', name: 'glob', summary: 'src-open/**', status: 'running'}]},
  ]);

  assert.equal(sections.length, 1);
  assert.equal(sections[0].kind, 'subagent');
  assert.equal(sections[0].entry.id, 's1');
  assert.equal(sections[0].entry.tools.length, 1);
});

test('src-open keeps the live plan as a stable visible section', () => {
  const first = buildSections([{id: 'tasks:current', kind: 'tasks', text: '计划', tasks: [todo('inspect', 'in_progress'), todo('verify', 'pending')]}]);
  const second = buildSections([{id: 'tasks:current', kind: 'tasks', text: '计划', tasks: [todo('inspect', 'completed'), todo('verify', 'in_progress')]}]);

  assert.equal(first[0].kind, 'tasks');
  assert.equal(second[0].kind, 'tasks');
  assert.equal(first[0].id, second[0].id);
  assert.equal((second[0] as any).tasks[0].status, 'completed');
});

test('src-open folds completed subagent into expandable turn summary', () => {
  const sections = buildSections([
    {id: 'p1', kind: 'prompt', text: 'fix subagent ui'},
    {id: 'a1', kind: 'action', text: 'task', detail: 'fix subagent ui', done: true, ok: true},
    {id: 's1', kind: 'subagent', text: 'fix card', agentType: 'code', model: 'deepseek', status: 'done', toolCount: 2, elapsed: 1.4, summary: 'updated src-open'},
    {id: 'sum1', kind: 'summary', text: 'Turn complete', toolCount: 1, paths: [], tokens: {inp: 0, out: 0, cache: 0}, expanded: true},
  ]);

  const summary = sections.find((section: any) => section.kind === 'summary') as any;
  assert.ok(summary, 'summary exists');
  const subagentSteps = summary.steps.filter((step: any) => step.type === 'subagent');
  assert.equal(subagentSteps.length, 1);
  assert.equal(subagentSteps[0].entry.agentType, 'code');
  assert.equal(sections.some((section: any) => section.kind === 'actions'), false);
});
