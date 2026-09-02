// Probe: what does OpenTUI deliver for Ctrl+Shift+E ?
process.env.DEBUG_SKIP_BACKEND = '1';
import {testRender, useKeyboard} from '@opentui/solid';

function Probe() {
  useKeyboard((event: any) => {
    console.log('KEY EVENT:', JSON.stringify({name: event.name, ctrl: event.ctrl, shift: event.shift, meta: event.meta, alt: event.alt, raw: event.raw, sequence: event.sequence}));
  });
  return <box width={10} height={3}><text>probe</text></box>;
}

const setup = await testRender(() => <Probe />, {width: 40, height: 8});
await setup.flush({maxPasses: 3});
await setup.renderOnce();
setup.mockInput.pressKey('e', {ctrl: true, shift: true});
setup.mockInput.pressKey('e', {ctrl: true});
await new Promise(resolve => setTimeout(resolve, 100));
await setup.flush({maxPasses: 3});
await setup.renderOnce();
console.log('===== END =====');
process.exit(0);
