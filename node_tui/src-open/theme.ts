// Shared color tokens for the TUI. Kept in one place so the welcome page,
// usage header, transcript and overlays stay visually consistent.
export const C = {
  primary: '#6f6fff',
  secondary: '#a9aeff',
  accent: '#4b3fe3',
  error: '#e8463a',
  warning: '#efaa17',
  success: '#1dc981',
  info: '#27d2bf',
  textMuted: '#a1a1aa',
  text: '#e5e5e5',
} as const;

// Bright (亮色系) accents used by the welcome page's daily quote. Each day
// cycles to the next color so the panel stays fresh but never loud.
export const BRIGHT = {
  yellow: '#ffd75f',
  aqua: '#5fd7ff',
  mint: '#7dffc2',
  coral: '#ff9e9e',
  lilac: '#d7afff',
  peach: '#ffc07a',
} as const;

export const BRIGHT_CYCLE = [BRIGHT.yellow, BRIGHT.aqua, BRIGHT.mint, BRIGHT.coral, BRIGHT.lilac, BRIGHT.peach] as const;
