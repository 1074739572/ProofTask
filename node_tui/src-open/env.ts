// Terminal capability hints for the opentui native renderer. Imported for its
// side effects and MUST stay the first import in index.tsx (ESM imports run in
// order, so this executes before @opentui code reads the environment).
//
// opentui's native core decides color support from a small whitelist of
// forwarded env vars (TERM / COLORTERM / WT_SESSION / TERM_PROGRAM / …) plus
// capability queries. A plain Windows console window has none of those
// variables set and conhost's query answers are unreliable, so detection can
// end up with neither rgb nor ansi256 — which is when every color renders as
// plain white text.
//
// Any Windows 10+ console host understands 256-color VT sequences, and 24-bit
// color has worked in conhost since Windows 10 1607 (PyCharm / VS Code /
// Windows Terminal all support it too). So when the environment advertises
// nothing, we advertise it ourselves. Real terminals that set their own
// variables (WT_SESSION, COLORTERM, TERM_PROGRAM, TERM) are left untouched.
// HARNESS_TUI_NO_ENV_HINTS=1 opts out (debugging detection issues).
if (process.platform === 'win32' && !process.env.HARNESS_TUI_NO_ENV_HINTS) {
  if (!process.env.COLORTERM && !process.env.WT_SESSION && !process.env.TERM_PROGRAM) {
    process.env.COLORTERM = 'truecolor';
  }
  if (!process.env.TERM) {
    process.env.TERM = 'xterm-256color';
  }
}

export {};
