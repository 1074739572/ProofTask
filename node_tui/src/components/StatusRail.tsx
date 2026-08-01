import React from 'react';
import {Box, Text} from 'ink';
import type {RunPhase} from '../types.js';
import {formatElapsed} from '../hooks/useSpinner.js';

function phaseLabel(phase: RunPhase): string {
  switch (phase) {
    case 'preparing': return 'prepare';
    case 'calling_model': return 'model';
    case 'streaming_response': return 'writing';
    case 'tool_running': return 'tool';
    case 'blocked': return 'blocked';
    case 'interrupted': return 'stopped';
    default: return 'idle';
  }
}

function short(value: string, max: number): string {
  if (!value) return '—';
  return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

export function StatusRail({
  model,
  mode,
  phase,
  running,
  frame,
  elapsed,
  width,
}: {
  model: string;
  mode: string;
  phase: RunPhase;
  running: boolean;
  frame: string;
  elapsed: number;
  width: number;
}) {
  const color = phase === 'blocked' ? 'red' : running ? 'yellow' : 'gray';
  const inner = Math.max(8, width - 2);
  return (
    <Box flexDirection="column" width={width} paddingRight={1}>
      <Text color="gray">model</Text>
      <Text color="cyan">{short(model || 'model', inner)}</Text>
      <Text color="gray">mode</Text>
      <Text color="cyan">{short(mode || 'mode', inner)}</Text>
      <Text color="gray">state</Text>
      <Text color={color}>{running ? `${frame} ` : ''}{phaseLabel(phase)}</Text>
      {running ? <Text color="gray">{formatElapsed(elapsed)}</Text> : null}
    </Box>
  );
}
