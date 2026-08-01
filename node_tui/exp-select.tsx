import {Box, Text, Select} from '@opentui/core';
import {createTestRenderer as ctr} from '@opentui/core/testing';

async function show(name: string, build: (root: any) => void, w = 60, h = 16) {
  const setup = await ctr({width: w, height: h});
  const root = Box({width: w, height: h, flexDirection: 'column'});
  build(root);
  setup.renderer.root.add(root);
  await setup.flush({maxPasses: 8});
  await setup.renderOnce();
  console.log(`===== ${name} =====`);
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
  setup.renderer.destroy();
}

const opts = [
  {name: 'Allow once', description: 'just this time', value: 'allow'},
  {name: 'Allow session', description: 'remember until exit', value: 'session'},
  {name: 'Deny', description: 'block this call', value: 'deny'},
];

// S1: static select with options at construction
await show('S1: static select', (root) => {
  const box = Box({border: true, borderStyle: 'rounded', borderColor: 'cyan', height: 8, paddingX: 1, flexDirection: 'column'});
  const sel = Select({flexGrow: 1, options: opts, showDescription: true, showScrollIndicator: true});
  box.add(sel);
  root.add(box);
});

// S2: select added AFTER first layout pass (dynamic insert simulation)
await show('S2: late-added select', (root) => {
  const box = Box({border: true, borderStyle: 'rounded', borderColor: 'cyan', height: 8, paddingX: 1, flexDirection: 'column'});
  root.add(box);
  setTimeout(() => {
    const sel = Select({flexGrow: 1, options: opts, showDescription: true, showScrollIndicator: true});
    box.add(sel);
    box.requestRender?.();
  }, 50);
});

// S3: static select, options assigned via setter AFTER construction
await show('S3: options via setter', (root) => {
  const box = Box({border: true, borderStyle: 'rounded', borderColor: 'cyan', height: 8, paddingX: 1, flexDirection: 'column'});
  const sel = Select({flexGrow: 1, showDescription: true});
  sel.options = opts;
  box.add(sel);
  root.add(box);
});

await new Promise(r => setTimeout(r, 200));
process.exit(0);
