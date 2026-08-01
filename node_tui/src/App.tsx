import React, {useCallback, useEffect, useMemo, useReducer, useRef, useState} from 'react';
import {Box, useApp, useInput} from 'ink';
import {startBackend, type Backend} from './backend.js';
import {initialState, reduceEvent} from './state.js';
import {HeaderStatus} from './components/HeaderStatus.js';
import {Timeline, transcriptLineCount} from './components/Timeline.js';
import {CommandPalette} from './components/CommandPalette.js';
import {Composer} from './components/Composer.js';
import {useTerminalSize} from './hooks/useTerminalSize.js';
import {useSpinner} from './hooks/useSpinner.js';

function isSlashCommand(text: string): boolean {
  return text.trimStart().startsWith('/');
}

function useBackend(dispatch: React.Dispatch<any>) {
  const backendRef = useRef<Backend | null>(null);
  React.useEffect(() => {
    backendRef.current = startBackend(
      event => dispatch(event),
      line => dispatch({type: 'log', level: 'warn', text: line}),
    );
    return () => backendRef.current?.stop();
  }, [dispatch]);
  return backendRef;
}

function App() {
  const [state, dispatch] = useReducer(reduceEvent, initialState);
  const [input, setInput] = useState('');
  const [scrollTopLine, setScrollTopLine] = useState(0);
  const [followBottom, setFollowBottom] = useState(true);
  const backendRef = useBackend(dispatch);
  const {exit} = useApp();
  const {rows, columns} = useTerminalSize();
  const compact = columns < 84 || rows < 20;
  const spinner = useSpinner(state.running || state.phase !== 'idle');

  const paletteRows = state.picker ? Math.min(state.picker.items.length + 3, Math.max(3, rows - 4)) : 0;
  const timelineRows = Math.max(1, rows - 3 - paletteRows);
  const lineCount = transcriptLineCount(state.items, columns);
  const maxScrollTop = Math.max(0, lineCount - timelineRows);

  useEffect(() => {
    if (followBottom) setScrollTopLine(maxScrollTop);
    else setScrollTopLine(v => Math.min(v, maxScrollTop));
  }, [lineCount, maxScrollTop, followBottom]);

  const submitPrompt = useCallback(() => {
    const text = input.trimEnd();
    if (!text.trim()) return;
    dispatch({type: 'local_user_message', text});
    setFollowBottom(true);
    setScrollTopLine(maxScrollTop);
    if (!isSlashCommand(text)) dispatch({type: 'thinking_start', phase: 'preparing'});
    backendRef.current?.send({type: 'user_message', text});
    setInput('');
  }, [input, backendRef, maxScrollTop]);

  const confirmPicker = useCallback(() => {
    const picker = state.picker;
    if (!picker) return;
    const selected = picker.items[picker.selected];
    if (!selected) return;
    const command = picker.id === 'model'
      ? `/model ${selected.id}`
      : picker.id === 'effort'
        ? `/effort ${selected.id}`
        : `/resume ${selected.id}`;
    dispatch({type: 'picker_close'});
    backendRef.current?.send({type: 'user_message', text: command, silent: true});
  }, [state.picker, backendRef]);

  useInput((inputChar, key) => {
    if (state.picker) {
      if (key.upArrow) { dispatch({type: 'picker_up'}); return; }
      if (key.downArrow) { dispatch({type: 'picker_down'}); return; }
      if (key.return) { confirmPicker(); return; }
      if (key.escape) { dispatch({type: 'picker_close'}); return; }
      return;
    }
    if (key.upArrow && !input) { setFollowBottom(false); setScrollTopLine(v => Math.max(0, v - 1)); return; }
    if (key.downArrow && !input) { setScrollTopLine(v => { const next = Math.min(maxScrollTop, v + 1); if (next >= maxScrollTop) setFollowBottom(true); return next; }); return; }
    if ((key as any).pageUp) { setFollowBottom(false); setScrollTopLine(v => Math.max(0, v - Math.max(3, Math.floor(timelineRows / 2)))); return; }
    if ((key as any).pageDown) { setScrollTopLine(v => { const next = Math.min(maxScrollTop, v + Math.max(3, Math.floor(timelineRows / 2))); if (next >= maxScrollTop) setFollowBottom(true); return next; }); return; }
    if ((key as any).end) { setFollowBottom(true); setScrollTopLine(maxScrollTop); return; }
    if (key.ctrl && inputChar === 'q') { backendRef.current?.send({type: 'exit'}); backendRef.current?.stop(); exit(); return; }
    if (key.ctrl && inputChar === 'c') { dispatch({type: 'thinking_end'}); backendRef.current?.send({type: 'interrupt'}); return; }
    if (key.ctrl && inputChar === 'l') { dispatch({type: 'ui_clear'}); setFollowBottom(true); setScrollTopLine(0); backendRef.current?.send({type: 'clear'}); return; }
  });

  const header = useMemo(() => (
    <HeaderStatus cwd={state.cwd} columns={columns} />
  ), [state.cwd, columns]);

  return (
    <Box flexDirection="column" width={columns} height={rows} paddingX={compact ? 0 : 1}>
      {header}
      <Timeline items={state.items} rows={timelineRows} columns={columns} scrollTopLine={scrollTopLine} />
      {state.picker ? <CommandPalette picker={state.picker} rows={paletteRows} /> : null}
      <Composer
        value={input}
        running={state.running}
        paletteOpen={Boolean(state.picker)}
        model={state.model}
        mode={state.mode}
        onChange={setInput}
        onSubmit={submitPrompt}
      />
    </Box>
  );
}

export default App;
