import React from 'react';
import {Box, Text} from 'ink';
import type {ChatItem} from '../types.js';
import {ActionRow} from './ActionRow.js';

function truncate(text: string, max: number): string {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  return clean.length <= max ? clean : clean.slice(0, Math.max(0, max - 1)) + '…';
}

export function TimelineItem({item, columns, compact}: {item: ChatItem; columns: number; compact: boolean}) {
  const textMax = Math.max(24, columns * (compact ? 2 : 4));
  switch (item.kind) {
    case 'user':
      return <Text color="cyan">› {truncate(item.text, textMax)}</Text>;
    case 'assistant':
      return <Box flexDirection="column"><Text color="green" bold>Response</Text><Text>{truncate(item.text, textMax)}</Text></Box>;
    case 'streaming':
      return <Box flexDirection="column"><Text color="green" bold>Response</Text><Text>{truncate(item.text, textMax)}</Text></Box>;
    case 'intent':
      return <Text color="gray">› {truncate(item.text, Math.max(20, columns - 4))}</Text>;
    case 'tool':
      return <ActionRow tool={item.tool} columns={columns} compact={compact} />;
    case 'files':
      return <Text color="yellow">Files  {truncate(item.paths.join(', '), Math.max(20, columns - 8))}</Text>;
    case 'log':
      return <Text color={item.level === 'plain' ? 'gray' : 'yellow'}>{truncate(item.text, Math.max(20, columns * 2))}</Text>;
    case 'error':
      return <Text color="red">Blocked  {truncate(item.text, Math.max(20, columns * 2))}</Text>;
    case 'tasks':
    case 'thinking':
    default:
      return null;
  }
}
