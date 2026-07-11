#!/usr/bin/env bash
# One-line installer for context-governor (Claude Code):
#
#   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
#
# The whole engine is one stdlib-only Python file. This downloads it to
# ~/.context-governor/app/governor.py and runs its self-installer, which
# merges two hooks into ~/.claude/settings.json (timestamped backup first,
# existing hooks never clobbered) and puts a `governor` command on your PATH.
# Re-running updates in place.
set -euo pipefail

BASE="${CG_RAW_BASE:-https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main}"
DEST="${CG_HOME:-$HOME/.context-governor/app}"

command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required" >&2; exit 1; }

mkdir -p "$DEST"
curl -fsSL "$BASE/governor.py" -o "$DEST/governor.py"
curl -fsSL "$BASE/config.example.json" -o "$DEST/config.example.json"
chmod +x "$DEST/governor.py"

python3 "$DEST/governor.py" install --claude "$@"
