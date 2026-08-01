import {Box, Text, ScrollBox, Input} from '@opentui/core';
import {createTestRenderer as ctr} from '@opentui/core/testing';

async function show(name: string, build: (root: any) => void, w = 60, h = 14) {
  const setup = await ctr({width: w, height: h});
  const root = Box({width: w, height: h, flexDirection: 'column'});
  build(root);
  setup.renderer.root.add(root);
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log(`===== ${name} =====`);
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
  setup.renderer.destroy();
}

const header = (root: any) => {
  const h = Box({border: true, borderStyle: 'single', borderColor: 'cyan', paddingX: 1});
  h.add(Text({content: 'Harness  model / mode  idle', fg: 'cyan', wrapMode: 'none', truncate: true}));
  h.add(Text({content: 'cwd unavailable', fg: 'gray', wrapMode: 'none', truncate: true}));
  root.add(h);
};

await show('G2: EMPTY scrollbox', (root) => {
  header(root);
  const sb = ScrollBox({flexGrow: 1, minHeight: 0, verticalScrollbarOptions: {visible: true}});
  root.add(sb);
  const footer = Box({border: true, borderStyle: 'single', borderColor: 'gray', paddingX: 1});
  footer.add(Text({content: 'hint line', fg: 'gray'}));
  root.add(footer);
});
await show('G3: scrollbox scrollY=false (no scrollbars)', (root) => {
  header(root);
  const content = Box({});
  content.add(Text({content: 'hello world', fg: 'green'}));
  const sb = ScrollBox({flexGrow: 1, minHeight: 0, scrollY: false});
  sb.add(content);
  root.add(sb);
  const footer = Box({border: true, borderStyle: 'single', borderColor: 'gray', paddingX: 1});
  footer.add(Text({content: 'hint line', fg: 'gray'}));
  root.add(footer);
});
await show('G4: scrollbox first, header after', (root) => {
  const content = Box({});
  content.add(Text({content: 'hello world', fg: 'green'}));
  const sb = ScrollBox({flexGrow: 1, minHeight: 0});
  sb.add(content);
  root.add(sb);
  header(root);
});
await show('G5: scrollbox as ONLY child', (root) => {
  const content = Box({});
  content.add(Text({content: 'hello world', fg: 'green'}));
  const sb = ScrollBox({flexGrow: 1, minHeight: 0});
  sb.add(content);
  root.add(sb);
});
await show('G6: no scrollbox, text + Input row only', (root) => {
  header(root);
  const body = Box({flexGrow: 1, minHeight: 0});
  body.add(Text({content: 'hello world', fg: 'green'}));
  root.add(body);
  const footer = Box({border: true, borderStyle: 'single', borderColor: 'gray', paddingX: 1});
  const row = Box({height: 1, flexDirection: 'row'});
  row.add(Text({content: '› ', fg: 'cyan'}));
  row.add(Input({flexGrow: 1, placeholder: 'Ask anything…'}));
  footer.add(row);
  footer.add(Text({content: 'hint line', fg: 'gray'}));
  root.add(footer);
});
process.exit(0);
