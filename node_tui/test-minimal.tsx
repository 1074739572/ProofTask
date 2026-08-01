import {render} from '@opentui/solid';

console.error('Starting minimal render test...');
try {
  const result = render(() => <text fg="green">Hello TUI!</text>, {exitOnCtrlC: true, targetFps: 30});
  console.error('Render called, result:', typeof result);
  result.catch((e: unknown) => { console.error('Render promise rejected:', e); process.exit(1); });
} catch (e) {
  console.error('Render threw synchronously:', e);
  process.exit(1);
}
