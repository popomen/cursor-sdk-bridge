#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${CURSOR_FALLBACK_PYTHON:-"${HOME}/.codex/cursor-sdk2api/venv/bin/python"}
export PYTHONDONTWRITEBYTECODE=1
exec "$python_bin" -m cursor_bridge.claude_switch "$@"
