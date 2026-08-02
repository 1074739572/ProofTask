import assert from 'node:assert/strict';
import {test} from 'node:test';
import {buildBackendArgs, initialWorkspace} from '../src/backend.js';

test('buildBackendArgs without cwd spawns default event-stream', () => {
  assert.deepEqual(buildBackendArgs(), ['main.py', '--event-stream']);
});

test('buildBackendArgs with cwd appends -C', () => {
  assert.deepEqual(buildBackendArgs('D:\\proj'), ['main.py', '--event-stream', '-C', 'D:\\proj']);
});

test('buildBackendArgs with empty cwd omits -C', () => {
  assert.deepEqual(buildBackendArgs(''), ['main.py', '--event-stream']);
});

test('initialWorkspace prefers HARNESS_WORKSPACE env', () => {
  const prev = process.env.HARNESS_WORKSPACE;
  try {
    process.env.HARNESS_WORKSPACE = 'D:\\from-env';
    assert.equal(initialWorkspace(), 'D:\\from-env');
  } finally {
    if (prev === undefined) delete process.env.HARNESS_WORKSPACE;
    else process.env.HARNESS_WORKSPACE = prev;
  }
});

test('initialWorkspace falls back to --workspace argv', () => {
  const prevEnv = process.env.HARNESS_WORKSPACE;
  const prevArgv = process.argv;
  try {
    delete process.env.HARNESS_WORKSPACE;
    process.argv = ['node', 'index.tsx', '--workspace', 'D:\\from-argv'];
    assert.equal(initialWorkspace(), 'D:\\from-argv');
  } finally {
    if (prevEnv === undefined) delete process.env.HARNESS_WORKSPACE;
    else process.env.HARNESS_WORKSPACE = prevEnv;
    process.argv = prevArgv;
  }
});

test('initialWorkspace returns undefined when unset', () => {
  const prevEnv = process.env.HARNESS_WORKSPACE;
  const prevArgv = process.argv;
  try {
    delete process.env.HARNESS_WORKSPACE;
    process.argv = ['node', 'index.tsx'];
    assert.equal(initialWorkspace(), undefined);
  } finally {
    if (prevEnv === undefined) delete process.env.HARNESS_WORKSPACE;
    else process.env.HARNESS_WORKSPACE = prevEnv;
    process.argv = prevArgv;
  }
});
