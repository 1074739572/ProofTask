// Codex-inspired transcript palette: let the terminal own the canvas. A single
// cyan accent identifies focus; color otherwise carries status, never decoration.
export const C = {
  primary: '#5ac8fa',
  secondary: '#9dd9ee',
  accent: '#5ac8fa',
  // On a dark terminal this is roughly a 12% lift, matching Codex's user cell.
  userCard: '#20252b',
  userCardBorder: '#5ac8fa',
  error: '#ff6b6b',
  warning: '#e6b566',
  success: '#72d6a2',
  info: '#5ac8fa',
  toolExec: '#e6b566',
  toolRead: '#9dd9ee',
  toolWrite: '#5ac8fa',
  toolWeb: '#c4a7e7',
  toolAgent: '#d6ad82',
  textMuted: '#8b949e',
  text: '#e6edf3',
} as const;

// Welcome quote accents stay quiet; the transcript never uses these decorative colors.
export const BRIGHT = {
  yellow: '#d8c17a',
  aqua: '#7fc8d8',
  mint: '#8cd0ad',
  coral: '#e89a94',
  lilac: '#b9c2ec',
  peach: '#d6ad82',
} as const;

export const BRIGHT_CYCLE = [BRIGHT.yellow, BRIGHT.aqua, BRIGHT.mint, BRIGHT.coral, BRIGHT.lilac, BRIGHT.peach] as const;
