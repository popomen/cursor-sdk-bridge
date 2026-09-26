# cursor-sdk-bridge

Cursor SDK Bridge exposes OpenAI Responses and Anthropic Messages through Cursor SDK. It includes Codex and Claude Code provider switching, a metadata dashboard, probes, and versioned deployment. Runtime state and credentials remain in their existing locations.

The command is `cursor-sdk-bridge`; the Python package is `cursor_sdk_bridge`.

```sh
cursor-sdk-bridge status
cursor-sdk-bridge serve codex --port 9889 --mode native
cursor-sdk-bridge serve claude --port 9890 --mode native
cursor-sdk-bridge serve dashboard --port 9891
cursor-sdk-bridge switch claude cursor
cursor-sdk-bridge switch claude restore
cursor-sdk-bridge probe --messages --port 9890
cursor-sdk-bridge deploy HEAD --install-only
cursor-sdk-bridge deploy HEAD
cursor-sdk-bridge restart claude
```

The default `native` engine streams thinking and text, keeps SDK tool callbacks alive across HTTP turns, and resumes direct successor conversations. `--mode reuse` keeps the JSON protocol with agent reuse; `--mode legacy` restores full-history JSON inference. All modes support detached inference and durable retry deduplication. See [engine design](docs/design.md).

In a checkout, use `python -m cursor_sdk_bridge` with the project's dependencies installed. `deploy` installs an exact Git commit and an independent virtual environment under `~/.local/share/cursor-sdk-bridge/<commit>`. It selects `current`, installs a command under `~/.local/bin`, and enables three regular user units. It does **not** start or restart services. `--install-only` leaves the selected release, command and units untouched for temporary-port validation. Running services use immutable release paths; editing the checkout cannot change them.

See [operations](docs/operations.md) for deployment, safe restart, client switching, rollback and state compatibility. The dashboard at `http://127.0.0.1:8791/` compares running and deployed Git commits. Real probes and switch preflights consume Cursor quota; deterministic tests do not.

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.codex/cursor-sdk2api/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Current sanitized experiment receipts belong in `docs/evidence/`. Earlier skill-era investigations remain in Git history; use the current operations guide for commands and recovery.
