// Verify: while idle (no turn running), the renderer must NOT emit periodic
// ANSI output. Before the fix, a 250ms setInterval forced a full-tree repaint
// every 250ms; now the clock only ticks while running().
process.env.DEBUG_SKIP_BACKEND = '1';
const {App} = await import('./App.tsx');
import {testRender} from '@opentui/solid';
const setup = await testRender(() => <App debugEntries={[]} />, {width: 100, height: 30});
await setup.flush({maxPasses: 5});
await setup.renderOnce();
await setup.waitForVisualIdle({quietFrames: 2, maxFrames: 30});
setup.externalOutput.clear();
// Wait 1.5s of pure idle (covers 6 old 250ms ticks).
await new Promise(r => setTimeout(r, 1500));
await setup.flush({maxPasses: 5});
const commits = setup.externalOutput.take();
let totalChars = 0;
for (const c of commits) totalChars += c.text.length;
console.log('IDLE commits:', commits.length, 'total chars:', totalChars);
for (const c of commits.slice(0, 5)) console.log('  commit:', JSON.stringify(c.text.slice(0, 120)));
process.exit(totalChars === 0 ? 0 : 1);
