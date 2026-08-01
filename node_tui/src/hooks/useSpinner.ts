import {useEffect, useRef, useState} from 'react';

const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export function useSpinner(active: boolean): {frame: string; elapsed: number} {
  const [tick, setTick] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(Date.now());

  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    started.current = Date.now();
    setElapsed(0);
    const timer = setInterval(() => {
      setTick(t => t + 1);
      setElapsed(Math.floor((Date.now() - started.current) / 1000));
    }, 120);
    return () => clearInterval(timer);
  }, [active]);

  return {frame: FRAMES[tick % FRAMES.length], elapsed};
}

export function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}
