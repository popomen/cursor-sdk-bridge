#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Claude Code instance with its own port, state and queue. 1800 s is the adapter cap; the queue limit matches
# the max deadline so a request waiting behind a full max run does not time out first.
exec "$script_dir/serve.sh" --port 8790 --state-dir "$HOME/.codex/cursor-sdk2api-claude" --timeout-max 1800 --queue-timeout 1800 "$@"
