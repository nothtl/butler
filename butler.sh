#!/usr/bin/env bash
# Butler launcher. Add ~/butler to your PATH, or symlink into ~/.local/bin.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# Prefer the user config (documented default); fall back to the repo config.
if [ -z "${BUTLER_CONFIG:-}" ]; then
  if [ -f "$HOME/.config/butler/config.toml" ]; then
    export BUTLER_CONFIG="$HOME/.config/butler/config.toml"
  else
    export BUTLER_CONFIG="$DIR/config/butler.toml"
  fi
fi
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$DIR/.venv/bin/python" -m butler.cli "$@"
