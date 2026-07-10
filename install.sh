#!/usr/bin/env bash
# One-line installer for context-governor (Claude Code):
#
#   curl -fsSL https://raw.githubusercontent.com/vsrox-cliqq/context-governor/main/install.sh | bash
#
# Clones (or updates) the repo into ~/.context-governor/app and merges the
# governor hooks into ~/.claude/settings.json (with a timestamped backup).
# Prefer the native plugin install if you use Claude Code plugins:
#
#   /plugin marketplace add vsrox-cliqq/context-governor
#   /plugin install context-governor@context-governor
set -euo pipefail

REPO="${CG_REPO:-https://github.com/vsrox-cliqq/context-governor}"
DEST="${CG_HOME:-$HOME/.context-governor/app}"

command -v git >/dev/null 2>&1 || { echo "error: git is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required" >&2; exit 1; }

if [ -d "$DEST/.git" ]; then
  echo "Updating existing install in $DEST"
  git -C "$DEST" pull --ff-only
else
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 "$REPO" "$DEST"
fi

python3 "$DEST/install.py" --claude
