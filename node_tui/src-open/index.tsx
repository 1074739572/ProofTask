import './env.ts';
import {render} from '@opentui/solid';
import {App} from './App.tsx';

render(() => <App />, {useMouse: true, exitOnCtrlC: false, targetFps: 60, maxFps: 60}).catch((error: unknown) => { console.error('OpenTUI render failed:', error); process.exit(1); });
