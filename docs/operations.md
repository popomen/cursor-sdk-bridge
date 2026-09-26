# Operating Cursor SDK Bridge

`cursor-bridge status` reads client configuration, daemon identity, service progress, request metadata and release versions. It does not make an inference request. The three instances are:

| Instance | Port | User unit | Existing state |
| --- | --- | --- | --- |
| Codex | 8789 | `cursor-bridge-codex.service` | `~/.codex/cursor-sdk2api` |
| Claude Code | 8790 | `cursor-bridge-claude.service` | `~/.codex/cursor-sdk2api-claude` |
| Dashboard | 8791 | `cursor-bridge-dashboard.service` | reads both instances |

The SDK key stays at `~/.codex/cursor-sdk-api-key`. Client backups and transaction journals stay at `~/.codex/cursor-fallback-state`. Deployment never moves these paths or changes their permissions. Logs contain request metadata, never prompt text, tool results, model output or credentials.

## Install and select a release

Commit and test the source before deployment. From the project checkout:

```sh
python -m cursor_bridge deploy HEAD --install-only
```

The command resolves a Git commit, archives its tracked files, creates a separate virtual environment, installs the pinned requirements and package, checks dependencies, and writes a release manifest. The installed dependency inventory is private to that release. Installation failures remove only the new incomplete release. Existing complete releases are reused.

Validate an installed release on temporary ports with separate experiment state; do not share a workspace with a running production instance:

```sh
~/.local/share/cursor-bridge/<commit>/venv/bin/cursor-bridge serve codex \
  --port 9889 --state-dir /tmp/cursor-bridge-validation-codex --mode legacy
~/.local/share/cursor-bridge/<commit>/venv/bin/cursor-bridge serve claude \
  --port 9890 --state-dir /tmp/cursor-bridge-validation-claude --mode reuse
```

Modes are explicit rollback choices: `legacy` preserves full-history JSON output; `reuse` and `native` select the newer paths when supported by that release. Use the adapter's `serve --help` for its current default and limits.

After validation, select the same commit:

```sh
python -m cursor_bridge deploy <commit>
```

This atomically selects `current`, registers three ordinary user unit files under `~/.config/systemd/user`, and enables them for `default.target`. It never starts, stops or restarts a service. Units refer directly to the immutable commit path, including the virtual environment, so selecting another release cannot change imports in existing processes. The dashboard indicates that a restart is needed when `/health.running_version` differs from the selected manifest. Checkout mtimes have no effect.

The command preserves unrelated command links and unit files by refusing to replace them. An install with `--install-only` changes no units or runtime links. `loginctl show-user "$USER" -p Linger` must report `yes` for unattended user services to start on machine boot; this host already has lingering enabled.

## Restart without interrupting work

```sh
cursor-bridge restart claude
cursor-bridge restart codex
cursor-bridge restart dashboard
```

For an adapter, the command first reads current progress and sockets. It then sends loopback-only `POST /admin/drain` with `{}`. Draining closes request admission and atomically reports `{"draining":true,"unfinished":0}` only when no request remains. Pending tools and detached runs count as unfinished. A busy response uses HTTP 409 and leaves admission open, so an existing run can still receive its tool results.

Only a confirmed zero unfinished count and zero established connections after the admin response closes permit `systemctl --user restart`. Unknown progress, unavailable drain support, pending work or open connections cause refusal. Any failure attempts `POST /admin/resume`; if resume fails, inspect `draining` in `/health` before retrying. The dashboard button uses the same guard. Restarting the dashboard only affects the UI process.

The first migration from the old `cursor-sdk2api*.service` transient units is a separate coordinated cutover: those units do not support the drain protocol. Validate the new release on temporary ports first; immediately before stopping an old adapter, verify `progress.active == null`, `progress.queued == 0`, and no established sockets. Start the replacement on the production port and verify its commit, limits and progress. If verification fails, stop the replacement and restore the old unit. Keep the old unit definitions and release until all acceptance checks pass. Never stop a running inference just because its client disconnected.

## Client switching

Claude Code keeps its existing settings backup and user-selected Cursor model tier:

```sh
cursor-bridge switch claude cursor
cursor-bridge switch claude restore
```

The first Cursor switch performs two real synthetic tool outputs at high effort, usually around one to two minutes. Repeating an already valid switch checks compatibility and updates managed settings without repeating inference. New sessions use the selected provider; existing sessions and Mew settings are untouched. Cursor model aliases keep `[1m]`; stream idle timeout, nonstreaming fallback, retry limits and `x-should-retry` handling remain part of the adapter compatibility contract.

Codex switching must run from an independent SSH terminal, after disconnecting Desktop and waiting for all daemon tasks to finish:

```sh
cursor-bridge switch codex cursor --restart-daemon
cursor-bridge switch codex restore --restart-daemon
```

The command preserves OpenAI credentials, uses the existing transaction journal, validates synthetic namespace calls, and restarts only the verified idle daemon. A switch is successful only when `runtime_matches_config: true` and no transaction remains. The catalog must point into a deployed release. Never execute the daemon restart from its own active Codex task. Existing conversations retain their original provider.

## Probes and rollback

```sh
cursor-bridge probe --port 9889
cursor-bridge probe --messages --port 9890
cursor-bridge probe --image --port 9889
```

A probe consumes one or two real SDK outputs; high effort typically takes one to two minutes. Announce expected quota use before running it. Probe metadata and sanitized receipts belong in `docs/evidence`; never store request content or credentials there. Authentication failures require a credential-validity check before more paid inference.

For an application rollback, deploy a known-good commit, then restart each idle adapter with the guarded command. Keep the same state paths. For a behavior rollback, start an instance with `--mode legacy` after the same idle checks. Provider restore commands restore the original managed client settings; they do not migrate existing conversations. A failed deployment restores previous unit file contents and the prior `current` link; it does not restart running processes.
