import React from 'react';
import {Box, Text} from 'ink';

function repoBase(cwd: string): string {
  if (!cwd) return '';
  return cwd.split(/[\\/]/).filter(Boolean).pop() || cwd;
}

export function HeaderStatus({cwd, columns}: {cwd: string; columns: number}) {
  const repo = repoBase(cwd);
  const text = repo ? `Harness · ${repo}` : 'Harness';
  const clipped = text.length > columns - 2 ? text.slice(0, Math.max(1, columns - 3)) + '…' : text;
  return (
    <Box>
      <Text color="cyan" bold>{clipped}</Text>
    </Box>
  );
}
