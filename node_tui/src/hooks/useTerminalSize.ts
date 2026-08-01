import {useEffect, useState} from 'react';
import {useStdout} from 'ink';

export type TerminalSize = {rows: number; columns: number};

export function useTerminalSize(): TerminalSize {
  const {stdout} = useStdout();
  const [size, setSize] = useState<TerminalSize>({rows: stdout?.rows || 30, columns: stdout?.columns || 100});

  useEffect(() => {
    if (!stdout) return;
    const onResize = () => setSize({rows: stdout.rows || 30, columns: stdout.columns || 100});
    onResize();
    stdout.on('resize', onResize);
    return () => { stdout.removeListener('resize', onResize); };
  }, [stdout]);

  return size;
}
