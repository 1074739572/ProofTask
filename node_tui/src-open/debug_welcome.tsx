// Offscreen probe: welcome brand panel — static final frame + sweep frames.
//   bun src-open/debug_welcome.tsx [width] [height]
import {testRender} from '@opentui/solid';
import {createSignal, Switch, Match} from 'solid-js';
import {WelcomeView, resetWelcomeSweepForTests} from './Welcome.tsx';

const width = Number(process.argv[2] || 100);
const height = Number(process.argv[3] || 30);

const frame = (setup: any, label: string) => {
  console.log(`===== ${label} =====`);
  console.log(setup.captureCharFrame().replace(/\s+$/gm, '').replace(/\n+$/, ''));
};

// 1. Static final frame (debug snapshot path: animate=false).
const staticSetup = await testRender(() => <WelcomeView
  width={width}
  height={height}
  quote="今天最好的提交，是把昨天的坑填上。"
  animate={false}
/>, {width, height});
await staticSetup.flush({maxPasses: 5});
await staticSetup.renderOnce();
frame(staticSetup, `STATIC FINAL ${width}x${height}`);
staticSetup.renderer.destroy();

// 2. Animated sweep: capture early / middle / done frames.
const liveSetup = await testRender(() => <WelcomeView
  width={width}
  height={height}
  quote="今天最好的提交，是把昨天的坑填上。"
  animate={true}
/>, {width, height});
await liveSetup.flush({maxPasses: 5});
await liveSetup.renderOnce();
frame(liveSetup, 'SWEEP t≈0ms');
await new Promise(resolve => setTimeout(resolve, 150));
await liveSetup.flush({maxPasses: 8});
await liveSetup.renderOnce();
frame(liveSetup, 'SWEEP t≈150ms');
await new Promise(resolve => setTimeout(resolve, 300));
await liveSetup.flush({maxPasses: 8});
await liveSetup.renderOnce();
frame(liveSetup, 'SWEEP t≈450ms (done)');
liveSetup.renderer.destroy();

// 3. Branch-stability regression: under the old plain `{fn()}` pattern a
// mid-sweep quote arrival disposed and recreated WelcomeView (sweep replayed,
// "logo 刷两遍"). Under Switch/Match the branch must survive a quote change.
let mountsPlain = 0;
let mountsSwitch = 0;
const TrackedPlain = (p: any) => { mountsPlain++; return <WelcomeView {...p} />; };
const TrackedSwitch = (p: any) => { mountsSwitch++; return <WelcomeView {...p} />; };

resetWelcomeSweepForTests();
const [quoteA, setQuoteA] = createSignal('旧引言');
const [showA] = createSignal(true);
// Old App pattern: plain `{fn()}` child whose body reads signals
// SYNCHRONOUSLY (old mainContent read goalSnapshot()/draftStatus() in branch
// conditions). Solid re-runs the insert effect when those change, disposing
// and recreating the subtree — hydration events replayed the sweep.
const plainContent = () => {
  const quote = quoteA(); // body-level read, like goalSnapshot() in old mainContent
  return showA() ? <TrackedPlain width={width} height={height} quote={quote} animate={true} /> : <box />;
};
const plainSetup = await testRender(() => <box height={height}>{plainContent()}</box>, {width, height});
await plainSetup.flush({maxPasses: 5});
await plainSetup.renderOnce();
setQuoteA('新引言到达，触发 mainContent 重跑');
await plainSetup.flush({maxPasses: 5});
await plainSetup.renderOnce();
plainSetup.renderer.destroy();

resetWelcomeSweepForTests();
const [quoteB, setQuoteB] = createSignal('旧引言');
const [showB] = createSignal(true);
const switchSetup = await testRender(() => <Switch fallback={<box />}>
  <Match when={showB()}>
    <TrackedSwitch width={width} height={height} quote={quoteB()} animate={true} />
  </Match>
</Switch>, {width, height});
await switchSetup.flush({maxPasses: 5});
await switchSetup.renderOnce();
setQuoteB('新引言到达，分支保持不变');
await switchSetup.flush({maxPasses: 5});
await switchSetup.renderOnce();
frame(switchSetup, 'SWITCH after quote swap (no remount)');
// Let this sweep run to completion so the latch engages for scenario 4.
await new Promise(resolve => setTimeout(resolve, 450));
await switchSetup.flush({maxPasses: 8});
await switchSetup.renderOnce();
switchSetup.renderer.destroy();
console.log(`MOUNTS plain-pattern=${mountsPlain} (expect 2: the bug) switch-pattern=${mountsSwitch} (expect 1: fixed)`);

// 4. Sweep latch: once one instance finished, a remounted instance renders
// the final frame immediately instead of replaying the reveal.
const latchedSetup = await testRender(() => <WelcomeView
  width={width}
  height={height}
  quote="闩锁后重挂载应直接显示最终帧。"
  animate={true}
/>, {width, height});
await latchedSetup.flush({maxPasses: 5});
await latchedSetup.renderOnce();
frame(latchedSetup, 'REMOUNT AFTER SWEEP (latched final frame)');
latchedSetup.renderer.destroy();
console.log('===== END =====');
process.exit(0);
