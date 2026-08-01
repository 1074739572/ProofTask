import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import path from 'node:path';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import type {UiEvent} from './types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '..', '..');

export type Backend = {
  process: ChildProcessWithoutNullStreams;
  send: (command: Record<string, unknown>) => void;
  stop: () => void;
};

export function startBackend(onEvent: (event: UiEvent) => void, onDiagnostic: (line: string) => void): Backend {
  const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const child = spawn(python, ['main.py', '--event-stream'], {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONIOENCODING: 'utf-8'},
  });

  const rl = readline.createInterface({input: child.stdout});
  rl.on('line', line => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      onEvent(JSON.parse(trimmed) as UiEvent);
    } catch {
      // Non-JSON lines are silenced (stderr already captured separately).
    }
  });

  const err = readline.createInterface({input: child.stderr});
  err.on('line', line => {
    const trimmed = line.trim();
    if (!trimmed) return;
    // Filter out common MCP / background noise
    const noise = [
      'MCP Server running on stdio',
      'MCP server running on stdio',
      'Server running on stdio',
      'pip install',
    ];
    if (noise.some(p => trimmed.includes(p))) return;
    onDiagnostic(trimmed);
  });

  child.on('exit', (code, signal) => {
    onEvent({type: 'log', level: code === 0 ? 'muted' : 'warn', text: `backend exited (${signal ?? code})`});
  });

  return {
    process: child,
    send(command) {
      if (child.killed || !child.stdin.writable) return;
      child.stdin.write(JSON.stringify(command) + '\n');
    },
    stop() {
      if (!child.killed) {
        child.stdin.write(JSON.stringify({type: 'exit'}) + '\n');
        setTimeout(() => {
          if (!child.killed) child.kill();
        }, 500);
      }
    },
  };
}
