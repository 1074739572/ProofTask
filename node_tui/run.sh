#!/bin/bash
# Launch the Ink TUI with the Python event-stream backend.
set -euo pipefail
cd "$(dirname "$0")"
cd ..
PYTHON="${PYTHON:-python}"
exec "$PYTHON" main.py --event-stream 2>tui_diag.log | node --import tsx/esm node_tui/src/index.tsx
