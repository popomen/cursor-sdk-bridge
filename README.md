# cursor-bridge

Cursor Bridge exposes OpenAI Responses and Anthropic Messages through Cursor SDK. It includes Codex and Claude Code provider switching, a metadata dashboard, probes, and versioned deployment. Runtime state and credentials remain in their existing locations.

```sh
cursor-bridge status
cursor-bridge serve codex --port 9889 --mode legacy
cursor-bridge serve claude --port 9890 --mode reuse
cursor-bridge serve dashboard --port 9891
cursor-bridge switch claude cursor
cursor-bridge switch claude restore
cursor-bridge probe --messages --port 9890
cursor-bridge deploy HEAD --install-only
cursor-bridge deploy HEAD
cursor-bridge restart claude
```

In a checkout, use `python -m cursor_bridge` with the project's dependencies installed. `deploy` installs an exact Git commit and an independent virtual environment under `~/.local/share/cursor-bridge/<commit>`. It selects `current`, installs a command under `~/.local/bin`, and enables three regular user units. It does **not** start or restart services. `--install-only` leaves the selected release, command and units untouched for temporary-port validation. Running services use immutable release paths; editing the checkout cannot change them.

See [operations](docs/operations.md) for deployment, safe restart, client switching, rollback and state compatibility. The dashboard at `http://127.0.0.1:8791/` compares running and deployed Git commits. Real probes and switch preflights consume Cursor quota; deterministic tests do not.

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.codex/cursor-sdk2api/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

`docs/archive/` preserves incident and experiment records from `codex-cursor-fallback`; commands in those historical records refer to the old skill layout. Current sanitized experiment receipts belong in `docs/evidence/`.
