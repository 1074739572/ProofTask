// Step-by-step probe of the /effort -> picker flow.
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';

const width = 100;
const height = 28;
const cap = (label: string) => {
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
const step = async () => { await new Promise(r => setTimeout(r, 80)); await setup.flush({maxPasses: 5}); await setup.renderOnce(); };
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
cap('INITIAL');

await setup.mockInput.typeText('/effort');
await step();
cap('TYPED /effort');

setup.mockInput.pressEnter();
await step();
cap('AFTER ENTER #1');

setup.mockInput.pressEnter();
await step();
cap('AFTER ENTER #2');

setup.mockInput.pressArrow('down');
await step();
cap('AFTER DOWN');

setup.mockInput.pressEnter();
await step();
cap('AFTER ENTER #3 (select)');
console.log('===== END =====');
process.exit(0);
