# cursor-sdk-bridge

Cursor SDK Bridge exposes OpenAI Responses and Anthropic Messages through Cursor SDK. A separate TraeX Astra Responses service supports Codex. It includes three-way Codex and two-way Claude Code provider switching, a metadata dashboard, probes, and versioned deployment. Runtime state and credentials remain in their existing locations.

The command is `cursor-sdk-bridge`; the Python package is `cursor_sdk_bridge`.

Both APIs expose all 20 Opus 5.5 variants: five efforts (`low`, `medium`, `high`, `xhigh`, `max`), two context windows, and two speeds. Replace `<effort>` in these IDs:

| Context | Speed | Model ID |
| --- | --- | --- |
| 1m | Standard | `claude-opus-5-5-<effort>` |
| 1m | Fast | `claude-opus-5-5-<effort>-fast` |
| 300k | Standard | `claude-opus-5-5-<effort>-300k` |
| 300k | Fast | `claude-opus-5-5-<effort>-300k-fast` |

The original high/xhigh/max IDs retain their 1m, standard-speed behavior. `GET /v1/models` lists every ID; `probe --model <ID>` checks a chosen variant.

The default `native` engine streams thinking and text, keeps SDK tool callbacks alive across HTTP turns, and resumes direct successor conversations. `--mode reuse` keeps the JSON protocol with agent reuse; `--mode legacy` restores full-history JSON inference. All modes support detached inference and durable retry deduplication. See [engine design](docs/design.md).

In a checkout, use `python -m cursor_sdk_bridge` with the project's dependencies installed. `deploy` installs an exact Git commit and an independent virtual environment under `~/.local/share/cursor-sdk-bridge/<commit>`. It selects `current`, installs a command under `~/.local/bin`, and enables four regular user units. It does **not** start or restart services. `--install-only` leaves the selected release, command and units untouched for temporary-port validation. Running services use immutable release paths; editing the checkout cannot change them.

The guide below covers everyday tasks. [Operations](docs/operations.md) explains deployment, restart guards, client switching, rollback and state compatibility in depth.

## Operations guide

Run these commands on the machine that hosts the services. If `cursor-sdk-bridge` is not on `PATH`, use `~/.local/bin/cursor-sdk-bridge`. Only probes, switch preflights (including the first `switch claude cursor`) and messages you send to a session make real SDK calls and consume Cursor quota; every other command reads metadata or manages processes.

| Instance | Port | User unit | Request log |
| --- | --- | --- | --- |
| Codex | 8789 | `cursor-sdk-bridge-codex.service` | `~/.codex/cursor-sdk2api/logs/requests.jsonl` |
| Claude Code | 8790 | `cursor-sdk-bridge-claude.service` | `~/.codex/cursor-sdk2api-claude/logs/requests.jsonl` |
| TraeX (Codex) | 8792 | `cursor-sdk-bridge-traex.service` | `~/.codex/traex-bridge/logs/requests.jsonl` |
| Dashboard | 8791 | `cursor-sdk-bridge-dashboard.service` | none |

### Check status and open the dashboard

1. Run `cursor-sdk-bridge status`. It prints clients, instances and versions as JSON and makes no inference request.
2. Open the dashboard at http://127.0.0.1:8791/. From another computer, forward the port first with `ssh -L 8791:127.0.0.1:8791 <host>`. The same data is served at `/api/status`.
3. When the dashboard says an instance needs a restart, its `running_version` differs from the deployed release. Restart it as described in [Restart an instance safely](#restart-an-instance-safely).

### See what keeps an instance busy

A refused restart means the instance still has unfinished work. List it (use port 8790 for Claude Code):

```sh
curl -s --noproxy '*' http://127.0.0.1:8789/health | python3 -c 'import json, sys
d = json.load(sys.stdin); p = d["progress"]
print(d["running_version"][:7], "unfinished", d["unfinished"], "queued", p["queued"],
      "pending_tools", p["pending_tools"], "max_concurrency", d["limits"]["max_concurrency"])
for r in p["runs"]: print(" ", r["state"], r["model"], "running_s", r["running_s"], "idle_s", r["idle_s"])'
```

- `running` is SDK inference in progress. It finishes even after its client has gone.
- `awaiting_tool_results` is a run waiting for its client to return tool output. If none arrives, it fails after `limits.pending_timeout` seconds (600 on this host).
- Closing a client window cancels neither kind. The session you are working in counts as well: a Codex task keeps 8789 busy and a Claude Code session keeps 8790 busy while they run.
- Runs do not carry session names; match them by model and `running_s`.

### Restart an instance safely

1. Use a terminal that the instance does not serve: restart 8789 from an SSH terminal or a Claude Code session, never from a Codex task, and the reverse for 8790. This restarts only the bridge process; Codex Desktop, the Codex daemon and Claude Code keep running.
2. Run `cursor-sdk-bridge restart codex` (or `claude`, `dashboard`).
3. `"result": "restarted"` means the new process answered `/health` with the deployed version. The first request of each session afterwards rebuilds its SDK conversation, so it takes longer.
4. `"result": "refused"` means work is still open and nothing was restarted. Wait and retry, or let this loop retry every minute until the instance is idle:

   ```sh
   while ! out=$(cursor-sdk-bridge restart codex) && echo "$out" | grep -q '"result": "refused"'; do
     date '+%T still busy, retrying in 60 s'; sleep 60
   done; echo "$out"
   ```

   A heavily used instance can stay busy for hours. Finishing or closing its sessions shortens the wait.
5. `--force` drops the listed work and restarts anyway. Use it only after every client of that instance has exited.

### Deploy a new version

1. Commit the change and run the [tests](#tests). Documentation-only commits need no deployment.
2. Optionally run `cursor-sdk-bridge deploy HEAD --install-only` and validate on temporary ports as in [operations](docs/operations.md#install-and-select-a-release).
3. Run `cursor-sdk-bridge deploy HEAD`. It installs and selects the commit but restarts nothing.
4. [Restart](#restart-an-instance-safely) each adapter when it is idle. `status` and the dashboard then show matching running and deployed versions.
5. If Codex currently uses Cursor, `status` now reports `codex.catalog_path_upgrade_required: true` because Codex's model catalog still points into the previous release. Run `switch codex cursor --restart-daemon` at the next convenient [Codex switch](#switch-codex-between-openai-and-cursor). Until then, keep that release directory: `model_catalog_json` in `~/.codex/config.toml` refers to it.

### Change concurrency

Each instance runs up to `CURSOR_FALLBACK_MAX_CONCURRENCY` SDK inferences at once: 8 by default for Claude Code, 3 for Codex, and at most 8. Each instance also admits at most 16 running or queued requests. A higher limit uses quota faster. Set it with a systemd drop-in; deployments rewrite only the main unit files and leave drop-ins in place.

1. Create the override (Codex example):

   ```sh
   mkdir -p ~/.config/systemd/user/cursor-sdk-bridge-codex.service.d
   printf '[Service]\nEnvironment=CURSOR_FALLBACK_MAX_CONCURRENCY=8\n' \
     > ~/.config/systemd/user/cursor-sdk-bridge-codex.service.d/max-concurrency.conf
   systemctl --user daemon-reload
   ```

2. [Restart](#restart-an-instance-safely) the instance when it is idle. Codex Desktop does not need a restart.
3. Check that `/health` on that port reports `limits.max_concurrency` as 8.

To undo, delete the `.conf` file, run `systemctl --user daemon-reload` and restart the instance again.

### Switch Claude Code

```sh
cursor-sdk-bridge switch claude cursor    # new sessions use the bridge on 8790
cursor-sdk-bridge switch claude restore   # new sessions use the original provider
```

- The first switch to Cursor runs two real high-effort inferences, usually one to two minutes. Repeating it only checks and updates settings.
- Only new `claude` sessions change; running sessions keep their provider. Start a new session to verify.
- The backup `~/.codex/cursor-fallback-state/claude-code.json` contains the original credentials. Do not print or edit it.

### Switch Codex between OpenAI, Cursor and TraeX

The switch restarts the Codex daemon, so run it yourself from an independent SSH terminal, never from a Codex task.

1. Finish every Codex task, including the one you are using, and disconnect Codex Desktop from this host.
2. Run one of these. If a task is still running, the command does not restart the daemon; finish the task and run it again.

   ```sh
   cursor-sdk-bridge switch codex traex --restart-daemon     # Astra through 8792
   cursor-sdk-bridge switch codex openai --restart-daemon   # back to the original (OpenAI) provider
   cursor-sdk-bridge switch codex cursor --restart-daemon    # to Cursor through 8789
   ```

3. Run `cursor-sdk-bridge status` and check `codex.provider` is the target, `codex.runtime_matches_config` is `true` and `codex.transaction_pending` is `false`.
4. Reconnect Desktop, create a **new task** and send a short message.

After a switch:

- Existing tasks keep their provider. Old Cursor and TraeX tasks continue using their respective services, so keep those services running after restoring OpenAI. `restore` remains an alias for `openai`.
- Changing the model inside an existing task does not change its provider and fails with `model is not supported when using Codex with a ChatGPT account`. Create a new task instead.
- Keep `~/.codex/auth.json`.

### TraeX Astra service

`cursor-sdk-bridge serve traex` runs the pinned TraeX protocol core on `127.0.0.1:8792`.
It exposes Responses and model metadata, with only `GPT-6-Astra` and `GPT-6-Astra[1m]`.
New TraeX configurations default to Astra 1m at medium effort; current catalog efforts are low, medium,
high and xhigh. Unknown models fail without fallback. Claude Code settings are independent.

The managed service always requires the existing `~/.traex-bridge/api-key`, including on loopback.
Codex reads that file through its provider `auth.command`; no shell export is needed. Trae credentials
are obtained using the reference project's SSH/git mechanism, or its configured token file/command.
The original credential directory and standalone TraeX unit are preserved. Metadata logs use a separate directory.

Codex merges a remote catalog with its bundled models. To show only Astra, switching creates an immutable
catalog snapshot from the live metadata under `~/.codex/cursor-fallback-state/traex-catalogs/` and selects it
through `model_catalog_json`. Keep snapshots referenced by configuration or recovery journals.
Switch state v3 preserves the original OpenAI baseline through direct Cursor ↔ TraeX transitions;
existing v2 state is upgraded when first entering TraeX.

TraeX requests are stateless: disconnection cancels the upstream request, and a retry sends the full history.
The bridge only retries explicit admission rejection before the upstream accepts work; Codex automatic
request/stream retries are disabled in the managed TraeX provider. Upstream queue heartbeats keep streams open.
`status` and the dashboard report ongoing work, release identity, observed usage and delayed upstream quota.

```sh
cursor-sdk-bridge probe --traex          # 2 short real Astra outputs, synthetic tool round trip
cursor-sdk-bridge probe --traex --image  # 1 short real Astra output, synthetic image
cursor-sdk-bridge restart traex         # drain + idle/connection checks
```

Deploy builds the Node >=22 service with the committed npm lockfile and packages the bundle into the immutable
Python release. A checkout build uses `npm --prefix integrations/traex ci --ignore-scripts --no-audit --no-fund`
followed by `npm --prefix integrations/traex run build:bridge`. The repository copy is never a production entry point.
For first activation after deploy, use `systemctl --user start cursor-sdk-bridge-traex.service` and verify
`cursor-sdk-bridge status`; subsequent restarts use the guarded command above.

### Diagnose a failed or stuck session

1. Note when the error appeared, for example Claude Code's `API Error: Server error mid-response` or Codex's `stream disconnected before completion` and `Cursor SDK response failed (<label>)`.
2. List recent requests that did not complete (use the Codex log for Codex):

   ```sh
   tail -n 300 ~/.codex/cursor-sdk2api-claude/logs/requests.jsonl | python3 -c 'import json, sys
   for r in map(json.loads, sys.stdin):
       if r.get("outcome") != "completed":
           print(r["ts"], r["event"], r.get("outcome"), r.get("model"),
                 "duration_s", r.get("duration_s"), "queue_s", r.get("queue_s"), "prompt_bytes", r.get("prompt_bytes"))'
   ```

3. Match the record by time and act on its outcome:

   | Outcome | Action |
   | --- | --- |
   | `queue_timeout`, `queue_full` | Too many concurrent requests. Wait, or [raise concurrency](#change-concurrency). |
   | `deadline_expired`, `request_timeout` | Inference ran past its limit. Compare `prompt_bytes` with `/health.prompt_limits`; compact the session (`/compact` in Claude Code) or start a new one. |
   | `prompt_too_large`, `context_length_exceeded` | Rejected before reaching the SDK. Compact or start a new session. |
   | `upstream_error:<class>`, `upstream_incomplete` | Cursor-side failure. For authentication errors, verify the SDK key before any further paid call. |
   | `invalid_request` | Local validation failure named by `request_error`. Do not retry blindly. |
   | `orphan_cancelled` | Normal cleanup of a queued request whose client left. No action. |

4. If there is no failed record and the session just sits there, check whether its run is still listed (see [See what keeps an instance busy](#see-what-keeps-an-instance-busy)). If it is gone, type `continue` in the session; that sends a new real request.

The [ops skill](.agents/skills/cursor-sdk-bridge-ops/SKILL.md) lists further outcomes. Logs hold metadata only; `results.sqlite3` and SDK local history may hold content, so never paste or commit them.

### Probe an instance

Each probe makes one or two real SDK calls; at high effort that takes about one to two minutes.

```sh
cursor-sdk-bridge probe --port 8789              # Codex namespace tools
cursor-sdk-bridge probe --messages --port 8790   # Claude Code tool round trip
cursor-sdk-bridge probe --image --port 8789      # image input
cursor-sdk-bridge probe --messages --port 8790 --model claude-opus-5-5-low-fast
```

### Roll back

1. Run `cursor-sdk-bridge deploy <known-good-commit>`.
2. [Restart](#restart-an-instance-safely) each adapter when it is idle.

State paths stay the same. For a behavior-only rollback with `--mode legacy`, see [operations](docs/operations.md#probes-and-rollback).

### Tests

Deterministic tests make no SDK calls:

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.codex/cursor-sdk2api/venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

The TraeX core also requires `npm --prefix integrations/traex run typecheck` and `npm --prefix integrations/traex test`.

Current sanitized experiment receipts belong in `docs/evidence/`. Earlier skill-era investigations remain in Git history; use this guide and the operations document for commands and recovery.
