import {render} from '@opentui/solid';

console.error('[1] Importing App...');
let AppModule: any;
try {
  AppModule = await import('./src-open/App.tsx');
  console.error('[2] App imported. Keys:', Object.keys(AppModule));
} catch (e) {
  console.error('[2] FAILED to import App:', e);
  process.exit(1);
}

console.error('[3] Calling render...');
try {
  const p = render(() => <AppModule.App debugEntries={[]} />, {useMouse: true, exitOnCtrlC: false, targetFps: 30});
  console.error('[4] render() returned:', typeof p);
  if (p && typeof p.catch === 'function') {
    p.catch((e: unknown) => { console.error('[5] render promise REJECTED:', e); process.exit(1); });
  }
} catch (e) {
  console.error('[4] render() THREW:', e);
  process.exit(1);
}

process.on('uncaughtException', (e) => { console.error('[UNCAUGHT]:', e); process.exit(1); });
process.on('unhandledRejection', (e) => { console.error('[UNHANDLED REJECTION]:', e); process.exit(1); });
