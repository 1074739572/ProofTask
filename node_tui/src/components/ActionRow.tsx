import React from 'react';
import {Box, Text} from 'ink';
import type {ToolRecord} from '../types.js';

function truncate(text: string, max: number): string {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  return clean.length <= max ? clean : clean.slice(0, Math.max(0, max - 1)) + '…';
}

export function ActionRow({tool, columns, compact}: {tool: ToolRecord; columns: number; compact: boolean}) {
  const failed = tool.status === 'failed' || tool.status === 'blocked';
  const mark = tool.status === 'done' ? '✓' : tool.status === 'failed' ? '✕' : tool.status === 'blocked' ? '⊘' : '⠙';
  const color = tool.status === 'done' ? 'green' : failed ? 'red' : 'yellow';
  const summaryBudget = Math.max(16, columns - tool.name.length - 12);

  if (!failed || compact) {
    return (
      <Box>
        <Text color={color}>{mark}</Text>
        <Text color="gray"> action </Text>
        <Text color="cyan">{tool.name}</Text>
        {tool.summary ? <Text color="gray">  {truncate(tool.summary, summaryBudget)}</Text> : null}
      </Box>
    );
  }

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="red" paddingX={1}>
      <Box>
        <Text color="red">{mark}</Text>
        <Text color="red"> blocked </Text>
        <Text color="cyan">{tool.name}</Text>
        {tool.summary ? <Text color="gray">  {truncate(tool.summary, summaryBudget)}</Text> : null}
      </Box>
      {tool.error ? <Text color="red">{truncate(tool.error, Math.max(30, columns * 2))}</Text> : null}
    </Box>
  );
}
