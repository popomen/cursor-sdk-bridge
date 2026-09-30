# Operating Cursor SDK Bridge

`cursor-sdk-bridge status` reads client configuration, daemon identity, service progress, request metadata and release versions. It does not make an inference request. The three instances are:

| Instance | Port | User unit | Existing state |
| --- | --- | --- | --- |
| Codex | 8789 | `cursor-sdk-bridge-codex.service` | `~/.codex/cursor-sdk2api` |
| Claude Code | 8790 | `cursor-sdk-bridge-claude.service` | `~/.codex/cursor-sdk2api-claude` |
| Dashboard | 8791 | `cursor-sdk-bridge-dashboard.service` | reads both instances |

The SDK key stays at `~/.codex/cursor-sdk-api-key`. Client backups and transaction journals stay at `~/.codex/cursor-fallback-state`. Deployment never moves these paths or changes their permissions. Logs contain request metadata, never prompt text, tool results, model output or credentials.

The SDK requires a Cursor User API key, not the CLI OAuth token. Its native bridge defaults to proxychains, preferring the user's executable wrapper; `CURSOR_FALLBACK_PROXYCHAINS` can select an absolute executable path. A readable model catalog does not prove that the inference network route works.

## Install and select a release

Commit and test the source before deployment. From the project checkout:

```sh
python -m cursor_sdk_bridge deploy HEAD --install-only
```

The command resolves a Git commit, archives its tracked files, creates a separate virtual environment, installs the pinned requirements and package, checks dependencies, and writes a release manifest. The installed dependency inventory is private to that release. Installation failures remove only the new incomplete release. Existing complete releases are reused.

Validate an installed release on temporary ports with separate experiment state; do not share a workspace with a running production instance:

```sh
~/.local/share/cursor-sdk-bridge/<commit>/venv/bin/cursor-sdk-bridge serve codex \
  --port 9889 --state-dir /tmp/cursor-sdk-bridge-validation-codex --mode legacy
~/.local/share/cursor-sdk-bridge/<commit>/venv/bin/cursor-sdk-bridge serve claude \
  --port 9890 --state-dir /tmp/cursor-sdk-bridge-validation-claude --mode reuse
```

Modes are explicit rollback choices: `legacy` preserves full-history JSON output; `reuse` and `native` select the newer paths when supported by that release. Use the adapter's `serve --help` for its current default and limits.

After validation, select the same commit:

```sh
python -m cursor_sdk_bridge deploy <commit>
```

This atomically selects `current`, registers three ordinary user unit files under `~/.config/systemd/user`, and enables them for `default.target`. It never starts, stops or restarts a service. Units refer directly to the immutable commit path, including the virtual environment, so selecting another release cannot change imports in existing processes. The dashboard indicates that a restart is needed when `/health.running_version` differs from the selected manifest. Checkout mtimes have no effect.

The command preserves unrelated command links and unit files by refusing to replace them. An install with `--install-only` changes no units or runtime links. `loginctl show-user "$USER" -p Linger` must report `yes` for unattended user services to start on machine boot; this host already has lingering enabled.

## Restart without interrupting work

```sh
cursor-sdk-bridge restart claude
cursor-sdk-bridge restart codex
cursor-sdk-bridge restart dashboard
```

For an adapter, the command first reads current progress and sockets. It then sends loopback-only `POST /admin/drain` with `{}`. Draining closes request admission and atomically reports `{"draining":true,"unfinished":0}` only when no request remains. Pending tools and detached runs count as unfinished. A busy response uses HTTP 409 and leaves admission open, so an existing run can still receive its tool results.

Only a confirmed zero unfinished count and zero established connections after the admin response closes permit `systemctl --user restart`. Unknown progress, unavailable drain support, pending work or open connections cause refusal. Any failure attempts `POST /admin/resume`; if resume fails, inspect `draining` in `/health` before retrying. The dashboard button uses the same guard. Restarting the dashboard only affects the UI process.

A refusal lists what is still open: inference in progress, runs waiting for the client to return tool results (with the longest wait and the `pending_timeout` after which they fail on their own), and queued requests. Client disconnects cancel none of these, so a closed client window does not make an instance idle. `cursor-sdk-bridge restart <instance> --force` sends `POST /admin/drain?force=1`, which closes admission regardless of unfinished work, skips the zero-work and zero-connection checks, and restarts; it also restarts an instance whose admin endpoint no longer answers. It still needs the deployed release and waits for `/health` to report it, and its result names the dropped work. Use it only after every client of that instance has exited and the listed requests may be dropped. Never force-restart the instance serving the task that issues the command. The dashboard button never forces.

## Client switching

Claude Code keeps its existing settings backup and user-selected Cursor model tier:

```sh
cursor-sdk-bridge switch claude cursor
cursor-sdk-bridge switch claude restore
```

The first Cursor switch performs two real synthetic tool outputs at high effort, usually around one to two minutes. Repeating an already valid switch checks compatibility and updates managed settings without repeating inference, including upgrading the previous three-model list to all 20 variants. New sessions use the selected provider; existing sessions and Mew settings are untouched. Only 1m variants carry `[1m]` in Claude Code settings; 300k variants use bare IDs. Stream idle timeout, nonstreaming fallback, retry limits and `x-should-retry` handling remain part of the adapter compatibility contract. The Haiku slot, which runs Claude Code's background calls such as session titles, uses `claude-opus-5-5-low-fast[1m]`. When a release manages a new key, updating an earlier switch records that key's prior value in the backup, so restore returns it.

`[1m]` tells Claude Code to plan a larger context window; the alias selects SDK context, effort and speed. Unknown bare Claude aliases may retain Claude Code's conservative 200k planning window even when the SDK variant supports 300k. Codex's bundled catalog declares the corresponding 300k or 1m window. Keep the managed `API_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK`, `CLAUDE_CODE_MAX_RETRIES` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` values. Ping traffic alone does not necessarily reset its event watchdog. Queue wait, SDK inference and the total request guard have separate limits and errors (`queue_timeout`, `deadline_expired`, `request_timeout`); inspect `/health.limits` rather than copying historical timeout values. Each instance runs up to `--max-concurrency` SDK inferences at once (at most 8, env `CURSOR_FALLBACK_MAX_CONCURRENCY`; default 8 for Claude Code and 3 for Codex); later requests queue. An instance admits at most 16 running or queued requests and answers further ones with 429 `queue_full`. A run awaiting client tool results gives its slot to the next queued request. When the results arrive it takes a slot back at once, even above the limit, and new requests wait until inference drops below it. A queued request that no client awaits any more, because its connection closed and no retry joined it within 30 s, is cancelled and logged as `orphan_cancelled`; started inference always finishes, so a retry can still use its ledger result. `/health.progress.runs` lists every in-flight run and `active` is the oldest. A failed run retires the shared SDK bridge from new admissions; the bridge closes after its remaining runs finish.

Codex switching must run from an independent SSH terminal, after disconnecting Desktop and waiting for all daemon tasks to finish:

```sh
cursor-sdk-bridge switch codex cursor --restart-daemon
cursor-sdk-bridge switch codex restore --restart-daemon
```

The command preserves OpenAI credentials, uses the existing transaction journal, validates synthetic namespace calls, and restarts only the verified idle daemon. A switch is successful only when `runtime_matches_config: true` and no transaction remains. The catalog must point into a deployed release. Never execute the daemon restart from its own active Codex task. Existing conversations retain their original provider.

Restoring OpenAI keeps the bridge-created `[model_providers.cursor]` definition when the original configuration had none. If that table already existed, its original definition is restored. This lets stored Cursor threads still resolve their provider while new threads default to OpenAI. The original default model, model catalog, feature settings and credential policy are restored; retaining the provider definition does not migrate any conversation. Cursor threads still need the bridge service to answer requests.

If a pre-existing `cursor` definition points to another backend, restoring it also routes Cursor-bound threads back to that backend. One provider ID cannot preserve both routes; check this before using an existing custom definition. A restore already completed by an older release may have removed both the definition and its backup; the retention rule cannot reconstruct that lost state.

`Model provider cursor not found` means the effective provider registry cannot resolve a Cursor-bound thread. Check the loaded configuration and the thread's saved provider before changing anything. Do not rewrite thread history to hide the error. A successful default-provider switch alone does not prove that Desktop has reopened an old thread with its original model; verify those separately.

`The 'claude-opus-5-5-xhigh' model is not supported when using Codex with a ChatGPT account` can also occur after a successful switch: an existing OpenAI-bound thread may receive a Cursor model selection while keeping its original provider. Check the saved thread provider and model together. Return that OpenAI thread to its OpenAI model, reconnect Desktop after the switch, and create a **new task** for Cursor. Changing the model on an existing thread does not migrate its provider. Keep `auth.json`; this error alone does not establish a credential problem. The [isolated compatibility receipt](evidence/codex-provider-model-mismatch-20260926.json) records this incident and an app-server tool roundtrip against a stub backend. A [real new-task receipt](evidence/codex-new-task-20260926.json) separately verifies Cursor using explicit CLI overrides; it does not change or test the Desktop default provider.

## Probes and rollback

```sh
cursor-sdk-bridge probe --port 9889
cursor-sdk-bridge probe --messages --port 9890
cursor-sdk-bridge probe --image --port 9889
```

A probe consumes one or two real SDK outputs; high effort typically takes one to two minutes. Announce expected quota use before running it. Probe metadata and sanitized receipts belong in `docs/evidence`; never store request content or credentials there. Authentication failures require a credential-validity check before more paid inference.

`invalid_request` is a local adapter rejection. Its `request_error` log field identifies the failed validation, such as `pending_results_mismatch`; it is not an upstream SDK error. Inspect the pending batch before retrying or restarting.

`prompt_too_large` rejects a prompt before it reaches the SDK. Messages returns HTTP 400 `prompt is too long: N tokens > M maximum`; Responses returns `context_length_exceeded`. The global limit is 3 MiB of rendered prompt. The Claude instance also limits 1m-context models to 1,900,000 bytes and 300k-context models to 500,000 bytes. Claude Code history averages about 2.1 bytes per SDK token, so a 2.1 MB prompt already fills the 1M window: each cold run then re-sends the prompt after its tool-schema lookup until the 1800 s deadline, and Claude Code reports `Server error mid-response`. Each response reports usage in the same estimated tokens as this error, and the managed `CLAUDE_CODE_AUTO_COMPACT_WINDOW=750000` makes Claude Code 2.1.285 compact a 1m session at about 717k estimated tokens, roughly 1.64 MB of prompt; it may start summarizing in the background from about 584k. That leaves about 250 KB for the next tool results and for the compaction request itself. The 400 remains the fallback: Claude Code 2.1.284 then retried a prompt-too-long compaction with shorter histories until it fit; compacting a 1.9 MB history at max effort took about 8.5 minutes. A 300k variant compacts at the smaller of this window and the window Claude Code plans for its bare ID. By default that window is 200k, which compacts before the 500,000-byte limit; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` can raise it, and such sessions still compact through the 400. A rejected tool-result continuation also cancels the paused run it would have resumed. `/health.prompt_limits` reports the effective limit for each context window.

For an application rollback, deploy a known-good commit, then restart each idle adapter with the guarded command. Keep the same state paths. For a behavior rollback, start an instance with `--mode legacy` after the same idle checks. Provider restore commands restore the original managed client settings; they do not migrate existing conversations. A failed deployment restores previous unit file contents and the prior `current` link; it does not restart running processes.
