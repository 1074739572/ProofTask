import React from 'react';
import {Box, Text} from 'ink';
import type {WelcomeState} from '../types.js';
import {wrapText} from './TranscriptBuffer.js';

const ACCENT = 'cyan';
const ART_WIDTH = 20;

function smileyColor(row: string, index: number, total: number): string {
  if (index === 0 || index === total - 1) return ACCENT;
  if (row.includes('██████')) return 'red';
  if ((row.match(/██/g) || []).length >= 4) return 'yellow';
  return ACCENT;
}

function displayWidth(text: string): number {
  let width = 0;
  for (const ch of Array.from(text)) {
    width += (ch.codePointAt(0) || 0) >= 0x1100 ? 2 : 1;
  }
  return width;
}

function CenteredText({text, columns, color, bold}: {text: string; columns: number; color: string; bold?: boolean}) {
  const pad = Math.max(0, Math.floor((columns - displayWidth(text)) / 2));
  return (
    <Box>
      <Text>{' '.repeat(pad)}</Text>
      <Text color={color as any} bold={bold}>{text}</Text>
    </Box>
  );
}

/** Startup welcome page: centered smiley art + today's quote (mirrors the CLI). */
export function Welcome({welcome, rows, columns}: {welcome: WelcomeState; rows: number; columns: number}) {
  const pad = Math.max(0, Math.floor((columns - ART_WIDTH) / 2));
  const bodyWidth = Math.max(16, columns - 4);
  const quoteLines = welcome.quote ? wrapText(`「${welcome.quote}」`, bodyWidth) : [];

  const artRows = welcome.art.length > 0 ? welcome.art : [];
  const contentLines = artRows.length + (quoteLines.length ? quoteLines.length + 1 : 0) + 1;
  const extraTop = rows - contentLines > 4 ? 1 : 0;

  return (
    <Box flexDirection="column" height={rows}>
      {extraTop ? <Text> </Text> : null}
      {artRows.map((row, i) => (
        <Box key={i}>
          <Text>{' '.repeat(pad)}</Text>
          <Text color={smileyColor(row, i, artRows.length) as any} bold>{row}</Text>
        </Box>
      ))}
      {quoteLines.length ? (
        <>
          <Text> </Text>
          {quoteLines.map((line, i) => (
            <CenteredText key={i} text={line} columns={columns} color="yellow" bold />
          ))}
        </>
      ) : null}
      <Text color="gray">Ready. Type a prompt or open a command palette.</Text>
    </Box>
  );
}
