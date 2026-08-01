import React from 'react';
import {Box, Text} from 'ink';
import type {ChatItem} from '../types.js';
import {buildTranscriptLines} from './TranscriptBuffer.js';

export function Timeline({
  items,
  rows,
  columns,
  scrollTopLine,
}: {
  items: ChatItem[];
  rows: number;
  columns: number;
  scrollTopLine: number;
}) {
  const lines = buildTranscriptLines(items, columns);
  const showIndicator = lines.length > rows;
  const contentRows = Math.max(1, showIndicator ? rows - 1 : rows);
  const maxTop = Math.max(0, lines.length - contentRows);
  const top = Math.min(Math.max(0, scrollTopLine), maxTop);
  const visible = lines.slice(top, top + contentRows);
  const padded = [...visible];
  while (padded.length < contentRows) padded.push({text: '', color: 'gray'});

  const atTop = top <= 0;
  const atBottom = top >= maxTop;
  const indicator = atBottom
    ? `line ${Math.min(lines.length, top + contentRows)}/${lines.length} · latest`
    : atTop
      ? `line ${top + 1}/${lines.length} · ↓ newer · End latest`
      : `line ${top + 1}/${lines.length} · ↑ older · ↓ newer · End latest`;

  return (
    <Box flexDirection="column" height={rows}>
      {lines.length === 0 ? (
        <Text color="gray">Ready. Type a prompt or open a command palette.</Text>
      ) : (
        <>
          {padded.map((line, idx) => (
            <Text key={`${top}-${idx}`} color={line.color as any} bold={line.bold}>{line.text}</Text>
          ))}
          {showIndicator ? <Text color="gray">{indicator}</Text> : null}
        </>
      )}
    </Box>
  );
}

export function transcriptLineCount(items: ChatItem[], columns: number): number {
  return buildTranscriptLines(items, columns).length;
}
