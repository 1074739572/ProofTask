import {BoxRenderable, SyntaxStyle} from '@opentui/core';
import {testRender} from '@opentui/solid';
import {For} from 'solid-js';
import {alwaysSeparate, setPreLayoutSiblingMargin} from './layout.ts';

const syntax = SyntaxStyle.fromStyles({
  default: {fg: '#e5e5e5'},
  keyword: {fg: '#a9aeff', bold: true},
  string: {fg: '#1dc981'},
  comment: {fg: '#a1a1aa', italic: true},
  number: {fg: '#efaa17'},
  function: {fg: '#27d2bf'},
  variable: {fg: '#e5e5e5'},
});

const messages = [
  '# First response\n\nThis paragraph has **bold**, *italic*, `inline code`, and a [link](https://example.com).\n\n- alpha\n- beta\n- gamma',
  '## Table\n\n| Name | State |\n| --- | --- |\n| parser | ready |\n| viewport | tested |\n| footer | fixed |',
  '## Code\n\n```ts\nfunction answer(value: number) {\n  return value * 2;\n}\n```\n\n> The final marker must start at the sticky bottom.\n\nFINAL_MARKER',
];

function Message(props: {content: string}) {
  return <box
    flexShrink={0}
    paddingLeft={2}
    ref={(element: BoxRenderable) => {
      alwaysSeparate.add(element);
      setPreLayoutSiblingMargin(element, previous =>
        previous instanceof BoxRenderable && (previous.height > 1 || alwaysSeparate.has(previous)) ? 1 : 0,
      );
    }}
  >
    <markdown
      syntaxStyle={syntax}
      streaming
      internalBlockMode="top-level"
      tableOptions={{style: 'grid'}}
      content={props.content}
      fg="#e5e5e5"
      conceal
    />
  </box>;
}

const setup = await testRender(() =>
  <box width={64} height={20} flexDirection="column">
    <box height={3} flexShrink={0} border borderColor="#4b3fe3">
      <text fg="#6f6fff">HEADER_FIXED</text>
    </box>
    <scrollbox
      flexGrow={1}
      minHeight={0}
      stickyScroll
      stickyStart="bottom"
      viewportOptions={{paddingRight: 1}}
      verticalScrollbarOptions={{visible: true}}
    >
      <For each={messages}>{content => <Message content={content} />}</For>
    </scrollbox>
    <box height={2} flexShrink={0}>
      <text fg="#6f6fff">FOOTER_FIXED</text>
    </box>
  </box>,
  {width: 64, height: 20},
);

const frame = async () => {
  await setup.flush({maxPasses: 10});
  await setup.renderOnce();
  await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
  return setup.captureCharFrame();
};

const initial = await frame();
if (!initial.includes('HEADER_FIXED') || !initial.includes('FOOTER_FIXED')) {
  throw new Error('fixed header or footer was overwritten');
}
if (!initial.includes('FINAL_MARKER')) throw new Error('sticky bottom did not show the newest content');

let scrolled = initial;
let revealedOlderContent = false;
for (let attempt = 0; attempt < 12; attempt++) {
  await setup.mockMouse.scroll(30, 9, 'up');
  scrolled = await frame();
  if (!scrolled.includes('HEADER_FIXED') || !scrolled.includes('FOOTER_FIXED')) {
    throw new Error('scrolling overwrote the fixed header or footer');
  }
  if (scrolled.includes('First response') || scrolled.includes('Table')) {
    revealedOlderContent = true;
    break;
  }
}
if (scrolled === initial) throw new Error('mouse wheel did not change the scrollbox viewport');
if (!revealedOlderContent) throw new Error('mouse wheel did not reveal older markdown content');

console.log('SCROLLBOX_MARKDOWN_OK');
console.log(scrolled.replace(/\s+$/gm, '').replace(/\n+$/, ''));
syntax.destroy();
process.exit(0);
