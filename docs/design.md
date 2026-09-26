# Engine migration

The source adapter was migrated from the personal `codex-cursor-fallback` skill. Its original incident and experiment records are preserved under `docs/archive/`; historical commands there use the old layout.

The stateful design was informed by [Sunnyender-org/cursor-sdk2api](https://github.com/Sunnyender-org/cursor-sdk2api), inspected locally at commit `b4b53b628701f1eb455678911494cde58f51f476` (MIT). This project implements the ideas independently in Python; it does not copy that project's code. The Python SDK contract differs: custom tools are registered when creating or resuming an agent, not on `send`.

## Independently selectable engines

| Mode | SDK history | Tool transport | Content delivery |
| --- | --- | --- | --- |
| `legacy` | new agent, full history each request | model-written JSON | buffered validated output |
| `reuse` | resume only for a validated direct successor | model-written JSON | buffered validated output |
| `native` | same run across tools; resume ordinary successors | SDK custom-tool callback Futures | live thinking/text deltas |

Native built-in shell, file and task tools remain disabled. Only registered client callbacks can request external work. The client performs that work and returns results. Namespace names are mapped to SDK-safe aliases and restored in the public protocol. Images stay out of prompts and logs; new turns attach only their new images when resuming.

## Ownership and recovery

The service owns each inference, not the HTTP handler. Closing a socket does not cancel the job. Identical requests join an in-flight future, or replay a completed response from a private SQLite ledger. The key includes expanded history, request policy and engine mode; it ignores transport-only `stream`, `store` and `previous_response_id` fields. Successful results are retained for one hour by default, within a 256 MiB retained-payload limit. Expiry or capacity eviction makes a later request cold. Failed inference is not automatically retried.

`results.sqlite3` is sensitive recovery state containing response content and usage, created with mode 0600. It is separate from metadata-only request logs. Existing state directories and their permissions are preserved. `store:false` continues to disable the Responses `previous_response_id` cache; it does not disable the retry ledger or the SDK's own local history. Use `--no-ledger` to disable retry result retention.

Lineage indexes retain policy/history hashes and SDK agent IDs. A validated completion publishes one successor opportunity; the first direct successor consumes it atomically. A sibling branch, changed policy, compaction, lost index or unavailable SDK resume creates a new agent with complete history. No ambiguous failed send is retried.

Native callback Futures and run consumers outlive a public tool-call response. Pending calls have a deadline and contribute to the unfinished-work count. Cold recovery reconstructs history and replays recorded results for already-completed tool signatures, in occurrence order, without asking the client to repeat their effects.

## Streaming and operations

Live deltas use stable item IDs through final responses and durable replay. A partial stream that fails ends with an error, never a fabricated completion. Responses events follow the [OpenAI streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses); Messages emits thinking/text content-block deltas. The bridge does not fabricate upstream reasoning signatures; a local empty signature accompanies synthesized Messages thinking blocks and is ignored on input.

Deployment installs a committed release in its own directory and virtual environment. User units point at that commit, never the mutable source checkout. Restart first closes admission, confirms zero unfinished work and zero open connections, then restarts the unit. The dashboard compares deployed and running commits. See [operations](operations.md).

## Evidence boundaries

- `native-callback-fake.json`: real Python callback HTTP server, synthetic SDK driver, no inference.
- `native-callback-live.json`: one real high-effort run, callback survives the first public HTTP response and continues on the second; key validity checked first.
- Unit tests cover isolated protocol, engine, lifecycle and deployment contracts.
- A live callback receipt alone does not prove the full adapter, cache improvement, production deployment or client acceptance. Those require separate temporary-port and production receipts.
