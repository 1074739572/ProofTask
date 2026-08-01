import React from 'react';
import {Box, Text} from 'ink';
import TextInput from 'ink-text-input';

function label(model: string, mode: string): string {
  const m = model || 'model';
  const md = mode || 'mode';
  const raw = `${m}/${md}`;
  return raw.length > 28 ? raw.slice(0, 27) + '…' : raw;
}

export function Composer({
  value,
  running,
  paletteOpen,
  model,
  mode,
  onChange,
  onSubmit,
}: {
  value: string;
  running: boolean;
  paletteOpen: boolean;
  model: string;
  mode: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  return (
    <Box>
      <Text color="gray">{label(model, mode)} </Text>
      <Text color="cyan">{paletteOpen ? 'select› ' : '› '}</Text>
      {paletteOpen ? (
        <Text color="gray">Use ↑↓, Enter, Esc</Text>
      ) : (
        <TextInput
          value={value}
          onChange={onChange}
          onSubmit={onSubmit}
          placeholder={running ? 'agent is working…' : 'Ask anything…'}
        />
      )}
    </Box>
  );
}
