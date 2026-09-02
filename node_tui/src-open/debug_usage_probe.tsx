// Offscreen probe: mount UsageView alone to reproduce the orphan-text crash.
//   bun src-open/debug_usage_probe.tsx [width]
import {testRender} from '@opentui/solid';
import {createSignal, Switch, Match} from 'solid-js';
import {UsageView} from './UsageView.tsx';

const width = Number(process.argv[2] || 80);

// Scenario 1: standalone UsageView.
try {
  const setup = await testRender(() => <UsageView
    width={width}
    height={24}
    range={() => 7}
    revision={() => 0}
  />, {width, height: 24});
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log('SCENARIO 1 OK — standalone UsageView mounted');
  setup.renderer.destroy();
} catch (error) {
  console.error('SCENARIO 1 CRASH:', (error as Error).message);
}

// Scenario 2: UsageView inside a non-matching Match (App mainContent pattern).
let usageMounts = 0;
const TrackedUsage = (p: any) => { usageMounts++; return <UsageView {...p} />; };
try {
  const [show] = createSignal(false);
  const [usageOpen] = createSignal(false);
  const [welcome, setWelcome] = createSignal(true);
  // Mirror App: plain-function mainContent called as a dynamic child.
  const mainContent = () => <Switch fallback={<box><text>logview-fallback</text></box>}>
    <Match when={usageOpen()}>
      <TrackedUsage width={width} height={24} range={() => 7} revision={() => 0} />
    </Match>
    <Match when={show()}>
      <box><text>other-branch</text></box>
    </Match>
    <Match when={welcome()}>
      <box><text>welcome-branch</text></box>
    </Match>
  </Switch>;
  const setup = await testRender(() => <box flexDirection="column">{mainContent()}</box>, {width, height: 24});
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log(`SCENARIO 2 OK — usageMounts=${usageMounts} (0 = lazy, 1 = eager)`);
  // Flip a signal like startup hydration does, then re-check.
  setWelcome(true);
  await setup.flush({maxPasses: 5});
  await setup.renderOnce();
  console.log(`SCENARIO 2 after signal flip — usageMounts=${usageMounts}`);
  setup.renderer.destroy();
} catch (error) {
  console.error(`SCENARIO 2 CRASH (usageMounts=${usageMounts}):`, (error as Error).message);
  console.error((error as Error).stack);
}
process.exit(0);
