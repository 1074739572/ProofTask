import React from 'react';
import {Box, Text} from 'ink';
import type {PickerState} from '../types.js';

function windowItems<T>(items: T[], selected: number, max: number): {items: T[]; offset: number} {
  const safeMax = Math.max(1, max);
  const offset = Math.min(Math.max(0, selected - Math.floor(safeMax / 2)), Math.max(0, items.length - safeMax));
  return {items: items.slice(offset, offset + safeMax), offset};
}

export function CommandPalette({picker, rows}: {picker: PickerState; rows: number}) {
  const maxItems = Math.max(1, rows - 3);
  const shown = windowItems(picker.items, picker.selected, maxItems);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1} height={rows}>
      <Text color="cyan" bold>{picker.title}</Text>
      {shown.items.map((item, localIdx) => {
        const idx = shown.offset + localIdx;
        const active = idx === picker.selected;
        return (
          <Box key={item.id}>
            <Text color={active ? 'green' : 'gray'}>{active ? '▶ ' : '  '}{item.label}</Text>
            {item.detail ? <Text color="gray">  {item.detail}</Text> : null}
          </Box>
        );
      })}
      <Text color="gray">↑↓ select · Enter confirm · Esc cancel</Text>
    </Box>
  );
}
