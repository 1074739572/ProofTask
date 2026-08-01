import {render} from '@opentui/solid';
import fs from 'node:fs';

const log = (s: string) => fs.appendFileSync('debug.log', s + '\n', 'utf8');
fs.writeFileSync('debug.log', '', 'utf8');

log('[1] Starting...');
let AppModule: any;
try {
  AppModule = await import('./src-open/App.tsx');
  log('[2] App imported. Keys: ' + Object.keys(AppModule).join(','));
} catch (e: any) {
  log('[2] FAILED to import App: ' + (e?.stack || e));
  process.exit(1);
}

log('[3] Calling render...');
try {
  const p = render(() => <AppModule.App debugEntries={[]} />, {useMouse: true, exitOnCtrlC: false, targetFps: 30});
  log('[4] render() returned type: ' + typeof p);
  if (p && typeof p.catch === 'function') {
    p.catch((e: unknown) => { log('[5] render promise REJECTED: ' + (e as any)?.stack || e); process.exit(1); });
  }
  log('[6] Still alive after 100ms');
  setTimeout(() => log('[7] Still alive after 2s'), 2000);
} catch (e: any) {
  log('[4] render() THREW: ' + (e?.stack || e));
  process.exit(1);
}

process.on('uncaughtException', (e: any) => { log('[UNCAUGHT]: ' + (e?.stack || e)); });
process.on('unhandledRejection', (e: any) => { log('[UNHANDLED REJECTION]: ' + (e?.stack || e)); });
process.on('exit', (code) => log('[EXIT] code=' + code));
