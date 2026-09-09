#!/usr/bin/env bash
# Butler launcher. Add ~/butler to your PATH, or symlink into ~/.local/bin.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export BUTLER_CONFIG="${BUTLER_CONFIG:-$DIR/config/butler.toml}"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$DIR/.venv/bin/python" -m butler.cli "$@"
