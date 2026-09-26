# Native callback feasibility

`python -m cursor_bridge.probe_native` is a deterministic FakeSdk experiment.
It uses the installed `cursor_sdk==1.0.32` `ToolCallbackServer`, two parallel
`CustomTool.execute` callbacks, and two separate public HTTP requests. The
first public response closes while both callback futures remain pending. The
second request supplies synthetic results and the original background run
finishes. The receipt records metadata only. No credential or upstream
inference is needed. `native-callback-fake.json` records the result.

The fake test proves the Python callback boundary and ownership model. A
separate upstream experiment is required to verify the SDK runtime. After
announcing its approximate consumption, run it with:

```sh
python -m cursor_bridge.probe_native --live --effort high --timeout 180 \
  --output docs/evidence/native-callback-live.json
```

The live variant reuses `SDKBackend._start()` for the existing credential,
proxychains route, HTTP/1 transport, and owned bridge cleanup. A read-only
`client.me()` credential check happens before a single billable inference
run. The model receives a short synthetic prompt, makes one native custom
tool call, waits across two HTTP requests, and produces a fixed synthetic
response. Receipts omit raw errors, arguments, results, model output, and
identifiers. An empty temporary workspace isolates the experiment.

The first real experiment succeeded on 2026-09-26; see
`native-callback-live.json`. One callback remained pending after the first
HTTP response completed and resumed within the same run when the second
request delivered its result. The run finished in 38.025 seconds with a
matching synthetic response and complete terminal envelopes. It emitted
28 thinking deltas and 2 text deltas before termination. Usage was 10,790
input tokens, 403 output tokens, 5,191 cache-read tokens, and 5,593
cache-write tokens. The read-only credential preflight passed. This proves
the native tool bridge is feasible with Python SDK 1.0.32; the experiment
does not yet establish long-history cache improvements or client SSE
translation correctness.

`tests/test_native_backend.py` exercises the production backend with FakeSdk:
20 deterministic tests cover native deltas, direct-successor resume, branches,
namespace routing, images, parallel and sequential callback batches, delayed
callbacks arriving after an HTTP response closed, pending expiry and cleanup,
read-only authentication diagnosis, and cold replay of repeated historical
tools in FIFO order. A tool-result request containing additional user text
retires the paused run and rebuilds from the complete transcript, replaying
completed tools; its receipt reason is `tool_results_with_new_user_input`.
Native backend integration and comparative real-client benchmarks still need
separate receipts; the earlier live probe established callback feasibility.

Implementation facts from installed SDK source:

- Register `CustomTool(execute=..., input_schema=...)` through
  `AgentOptions(local=LocalAgentOptions(custom_tools=...))` at
  `create_agent()` or `resume_agent()`. Python `agent.send()` rejects
  custom tool registration; the TypeScript example cannot be copied here.
- Async execute requires `AsyncCursorClient`. The threaded callback server
  waits on the owning event loop's coroutine without a callback timeout.
  Keep that loop, client, callback future and run consumer alive independently
  of an individual public HTTP response. Enforce pending TTL in our code.
- Native tools use the SDK's `mcp` tool category. Allow `tools=["mcp"]`
  when registering client tools; disallow ambient shell/read/edit/task/web
  tools and use `setting_sources=[]`. No custom tools means `tools=[]`.
- Return MCP content mappings (`content` plus optional `isError`). Plain
  strings are normalized to text content by the Python callback server.
- `SendOptions(on_delta=...)` enables real `text-delta` and
  `thinking-delta` dataclasses. A background task must continue consuming
  `run.events()`; merely keeping the handle does not dispatch deltas.
- Ordinary successors should bind `resume_agent(agent_id, AgentOptions(...))`
  and send only the newest turn. Claim each predecessor once under a lock;
  forks, changed policies/tools/models and compressed history rebuild.
- Pending callbacks are process-local. After a restart, reconstruct the
  transcript and match completed tool calls by name plus canonical argument
  digest, retaining repeated occurrences as a FIFO. Return recorded results
  from callbacks before exposing any new client tool request. Validate that
  each supplied result refers to a known call and the latest pending batch.

The open source design was inspected in
`Sunnyender-org/cursor-sdk2api` (MIT); implementation here is independent.
