import {mkdtempSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {startBackend} from '../src/backend.js';

const probe = mkdtempSync(path.join(tmpdir(), 'tui_bridge_'));
writeFileSync(path.join(probe, 'data.csv'), 'a,b\n');

let completionOk = false;
let switchOk = false;
let stillAlive = false;

const backend = startBackend(
  event => {
    if (event.type === 'ready') {
      console.log('ready received');
      // Backend cwd is the repo root, so complete against a real repo file.
      backend.send({type: 'completion_request', text: '查看 @main', request_id: 't1'});
    }
    if (event.type === 'completion_result') {
      completionOk = Array.isArray(event.candidates) && event.candidates.length === 1 && event.candidates[0].endsWith('main.py');
      console.log('completion_result:', JSON.stringify(event.candidates));
      if (completionOk) {
        // In-process switch: backend stays alive, emits workspace_switched.
        backend.send({type: 'user_message', text: `/open ${probe}`, silent: true});
      }
    }
    if (event.type === 'workspace_switched') {
      switchOk = event.cwd === probe;
      stillAlive = backend.process.exitCode === null;
      console.log('workspace_switched:', event.cwd, '| alive:', stillAlive);
      backend.stop();
      setTimeout(() => process.exit(completionOk && switchOk && stillAlive ? 0 : 1), 300);
    }
  },
  () => {},
);

setTimeout(() => {
  console.log('TIMEOUT');
  backend.stop();
  process.exit(1);
}, 90000);
