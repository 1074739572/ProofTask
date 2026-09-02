// Offscreen probe: does the effort picker open on /effort and apply a selection?
//   bun src-open/debug_effort.tsx [width] [height]
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 28);

const frame = (setup: any, label: string) => {
  console.log(`===== ${label} =====`);
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
};

const setup = await testRender(() => <App
  debugEntries={[]}
  debugEffort={{
    value: 'off',
    label: 'Model default',
    options: [
      {name: 'Model default', description: 'no override', value: 'off'},
      {name: 'Low', description: 'light reasoning', value: 'low'},
      {name: 'Medium', description: 'balanced reasoning', value: 'medium'},
      {name: 'High', description: 'deep reasoning', value: 'high'},
    ],
  }}
  debugUsage={{input: 96100, output: 32300, cacheRead: 70153}}
/>, {width, height});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
frame(setup, `FRAME ${width}x${height} BEFORE`);

// No backend in debug mode -> no completion candidates, so the first Enter
// submits /effort directly and the local intercept opens the picker.
await setup.mockInput.typeText('/effort');
await setup.flush({maxPasses: 5});
await setup.renderOnce();
setup.mockInput.pressEnter(); // submit -> local intercept opens picker
await new Promise(resolve => setTimeout(resolve, 120));
await setup.flush({maxPasses: 5});
await setup.renderOnce();
frame(setup, 'AFTER /effort SUBMIT (picker should be visible)');

setup.mockInput.pressArrow('down');
await setup.flush({maxPasses: 5});
await setup.renderOnce();
frame(setup, 'AFTER DOWN (selection moves to Low)');

setup.mockInput.pressEnter(); // apply selection
await new Promise(resolve => setTimeout(resolve, 120));
await setup.flush({maxPasses: 5});
await setup.renderOnce();
frame(setup, 'AFTER ENTER (picker closed, toast + header label updated)');
console.log('===== END =====');
process.exit(0);
