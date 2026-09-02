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

// Brand gradient (welcome wordmark/logo ONLY — everywhere else color carries
// status, never decoration). Three stops in the cyan family around the
// primary accent; dsh-tui used the same recipe (#4D6BFE→#3982FF→#2498FF)
// for its DEEPSEEK HARNESS banner. Foreground-only, so it stays readable on
// both dark and light terminals; OpenTUI downsamples hex when the terminal
// lacks truecolor.
const BRAND_STOPS: readonly (readonly [number, number, number])[] = [
  [0x2f, 0x8f, 0xe8], // deep azure
  [0x5a, 0xc8, 0xfa], // primary cyan (C.primary)
  [0xa5, 0xe3, 0xff], // ice
];

/** Piecewise-linear interpolation across BRAND_STOPS; t in [0,1] → hex color. */
export function brandColorAt(t: number): string {
  const x = Math.max(0, Math.min(1, Number(t) || 0));
  const scaled = x * (BRAND_STOPS.length - 1);
  const i = Math.min(BRAND_STOPS.length - 2, Math.floor(scaled));
  const f = scaled - i;
  const a = BRAND_STOPS[i];
  const b = BRAND_STOPS[i + 1];
  const channel = (k: number) => Math.round(a[k] + (b[k] - a[k]) * f);
  return `#${([channel(0), channel(1), channel(2)] as number[]).map(v => v.toString(16).padStart(2, '0')).join('')}`;
}
