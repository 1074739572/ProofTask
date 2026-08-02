import assert from 'node:assert/strict';
import {test} from 'node:test';

import {buildSections} from '../src-open/sections.ts';

test('src-open keeps running subagent in a bordered section', () => {
  const sections = buildSections([
    {id: 's1', kind: 'subagent', text: 'scan ui path', agentType: 'explore', model: 'mimo', status: 'running', rounds: ['Round 1 · "looking"'], tools: [{id: 't1', name: 'glob', summary: 'src-open/**', status: 'running'}]},
  ]);

  assert.equal(sections.length, 1);
  assert.equal(sections[0].kind, 'subagent');
  assert.equal(sections[0].entry.id, 's1');
  assert.equal(sections[0].entry.tools.length, 1);
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
  assert.equal(summary.subagents.length, 1);
  assert.equal(summary.subagents[0].agentType, 'code');
  assert.equal(sections.some((section: any) => section.kind === 'actions'), false);
});
