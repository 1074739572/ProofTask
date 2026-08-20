import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import path from 'node:path';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import type {UiEvent} from './types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '..', '..');

export type BackendOptions = {
  cwd?: string;
  onState?: (state: 'connected' | 'disconnected' | 'reconnecting', detail?: {code?: number | null; signal?: string | null; error?: string}) => void;
};

export type Backend = {
  process: ChildProcessWithoutNullStreams;
  cwd?: string;
  send: (command: Record<string, unknown>) => boolean;
  stop: () => void;
};

/** Pure helper: build the python argv for a given workspace (or default root). */
export function buildBackendArgs(cwd?: string): string[] {
  const args = ['main.py', '--event-stream'];
  if (cwd) args.push('-C', cwd);
  return args;
}

/** Resolve the initial workspace from HARNESS_WORKSPACE or `--workspace <dir>`. */
export function initialWorkspace(): string | undefined {
  const fromEnv = process.env.HARNESS_WORKSPACE;
  if (fromEnv) return fromEnv;
  const flagIndex = process.argv.indexOf('--workspace');
  if (flagIndex >= 0 && process.argv[flagIndex + 1]) return process.argv[flagIndex + 1];
  return undefined;
}

export function startBackend(
  onEvent: (event: UiEvent) => void,
  onDiagnostic: (line: string) => void,
  options: BackendOptions = {},
): Backend {
  const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const child = spawn(python, buildBackendArgs(options.cwd), {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONIOENCODING: 'utf-8'},
  });
  options.onState?.('connected');

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

  child.on('error', error => {
    options.onState?.('disconnected', {error: error.message});
  });
  child.on('exit', (code, signal) => {
    options.onState?.('disconnected', {code, signal});
    onEvent({type: 'log', level: code === 0 ? 'muted' : 'warn', text: `backend exited (${signal ?? code})`});
  });

  return {
    process: child,
    cwd: options.cwd,
    send(command) {
      if (child.killed || child.exitCode !== null || !child.stdin.writable) return false;
      try {
        child.stdin.write(JSON.stringify(command) + '\n');
        return true;
      } catch {
        return false;
      }
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
