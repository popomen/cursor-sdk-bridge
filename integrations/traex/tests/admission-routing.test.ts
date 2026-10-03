import { strict as assert } from "node:assert";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { buildApp } from "../src/app.js";
import { loadAdmissionConfig, type Config } from "../src/config.js";
import { CatalogStore } from "../src/upstream/catalog.js";
import type { UpstreamClient } from "../src/upstream/client.js";
import { QuotaCache } from "../src/upstream/quota.js";
import { UsageStore } from "../src/upstream/usage.js";

const full = 'event: error\ndata: {"code":4050,"message":"queue full"}\n\n';
const ok = 'event: output\ndata: {"response":"OK"}\n\nevent: done\ndata: {"finish_reason":"stop"}\n\n';
const config: Config = {
  host: "127.0.0.1", port: 8317, apiKey: "test", defaultModel: "m",
  upstreamBaseUrl: "https://example.invalid", catalogBaseUrl: "https://example.invalid",
  traePpeEnv: "", credentialSource: "environment", traeToken: "test", traeTokenFile: "",
  traeTokenCommand: "", bytedcliBin: "", tokenCommandTimeoutMs: 1000, stateDir: "/tmp/unused",
  maxRequestBytes: 10000, upstreamHeaderTimeoutMs: 1000, streamIdleTimeoutMs: 100,
  responsesDeferLifecycle: false,
  logLevel: "silent", logRequests: false, dumpBody: false, trace: false,
  admission: { ...loadAdmissionConfig({}), initialDelayMs: 1, maxDelayMs: 2, waitTimeoutMs: 2000 },
};

function fixture(
  chat: (body: unknown, signal?: AbortSignal, principal?: string) => Promise<Response>,
  overrides: Partial<Config> = {},
) {
  const client = { chat, principalScope: async () => "principal" } as unknown as UpstreamClient;
  const catalog = new CatalogStore();
  catalog.set([{
    id: "m", config: "cfg", model: "m-dev", contextWindow: 10000, usageLimited: false,
    repoRestricted: false, superModel: false, feeLevel: 0, manualUsage: 0,
  }]);
  const usage = new UsageStore();
  return { usage, app: buildApp({
    config: { ...config, ...overrides }, client, catalog, usage, quota: new QuotaCache(client, catalog, usage),
  }) };
}

test("Responses admission route keeps request content and model fixed but uses fresh attempt identities", async (t) => {
  const bodies: Array<Record<string, unknown>> = [];
  const { app, usage } = fixture(async (body, _signal, principal) => {
    assert.equal(principal, "principal");
    bodies.push(body as Record<string, unknown>);
    return bodies.length <= 8 ? new Response(full) : new Response(ok);
  });
  t.after(() => app.close());
  const result = await app.inject({
    method: "POST", url: "/v1/responses", headers: { authorization: "Bearer test" },
    payload: { model: "m", input: "hello", stream: true },
  });
  assert.equal(result.statusCode, 200);
  assert.match(result.body, /response.completed/);
  assert.doesNotMatch(result.body, /response.failed/);
  assert.equal(bodies.length, 9);
  assert.equal(new Set(bodies.map((body) => body.session_id)).size, 9);
  assert.equal(new Set(bodies.map((body) => body.conversation_id)).size, 9);
  for (const body of bodies) {
    assert.equal(body.model_name, "m-dev");
    assert.equal(body.config_name, "cfg");
    assert.deepEqual(body.messages, bodies[0]!.messages);
  }
  assert.equal(usage.snapshot()[0]!.requests, 1);
});

test("concurrent tool continuations keep context, queue events, and responses request-local", async (t) => {
  const bodies: Array<{ task: string; body: Record<string, unknown> }> = [];
  const attempts = new Map<string, number>();
  const success = (task: string): Response => new Response([
    `event: request_wait_in_queue\ndata: ${JSON.stringify({
      position: task === "task-A" ? 21 : 34,
      queue_id: `queue-${task}`,
      request_uuid: `provider-${task}`,
    })}\n\n`,
    `event: output\ndata: ${JSON.stringify({ response: `answer-${task}` })}\n\n`,
    'event: done\ndata: {"finish_reason":"stop"}\n\n',
  ].join(""));
  const { app } = fixture(async (rawBody) => {
    const body = rawBody as Record<string, unknown>;
    const task = String(body["user_input"]);
    bodies.push({ task, body });
    const attempt = (attempts.get(task) ?? 0) + 1;
    attempts.set(task, attempt);
    return attempt === 1 ? new Response(full) : success(task);
  });
  t.after(() => app.close());

  const continuation = (task: string, thread: string, turn: string) => app.inject({
    method: "POST",
    url: "/v1/responses",
    headers: {
      authorization: "Bearer test",
      "x-codex-turn-metadata": JSON.stringify({
        session_id: thread,
        thread_id: thread,
        turn_id: turn,
        request_kind: "turn",
        turn_trigger: "user",
      }),
    },
    payload: {
      model: "m",
      stream: true,
      client_metadata: {
        session_id: thread,
        thread_id: thread,
        turn_id: turn,
      },
      input: [
        { type: "message", role: "user", content: [{ type: "input_text", text: task }] },
        { type: "function_call", call_id: `call-${task}`, name: "lookup", arguments: "{}" },
        { type: "function_call_output", call_id: `call-${task}`, output: `tool-${task}` },
      ],
    },
  });

  const [responseA, responseB] = await Promise.all([
    continuation("task-A", "thread-A", "turn-A"),
    continuation("task-B", "thread-B", "turn-B"),
  ]);

  assert.equal(responseA.statusCode, 200);
  assert.equal(responseB.statusCode, 200);
  assert.match(responseA.body, /answer-task-A/);
  assert.doesNotMatch(responseA.body, /answer-task-B|queue-task-B/);
  assert.match(responseA.body, /queue-task-A/);
  assert.match(responseB.body, /answer-task-B/);
  assert.doesNotMatch(responseB.body, /answer-task-A|queue-task-A/);
  assert.match(responseB.body, /queue-task-B/);
  assert.deepEqual(Object.fromEntries(attempts), { "task-A": 2, "task-B": 2 });

  const aBodies = bodies.filter(({ task }) => task === "task-A").map(({ body }) => body);
  const bBodies = bodies.filter(({ task }) => task === "task-B").map(({ body }) => body);
  assert.equal(aBodies.length, 2);
  assert.equal(bBodies.length, 2);
  assert.deepEqual(aBodies[0]!["messages"], aBodies[1]!["messages"]);
  assert.deepEqual(bBodies[0]!["messages"], bBodies[1]!["messages"]);
  assert.notDeepEqual(aBodies[0]!["messages"], bBodies[0]!["messages"]);
  assert.equal(new Set(bodies.map(({ body }) => body["session_id"])).size, 4);
  assert.equal(new Set(bodies.map(({ body }) => body["conversation_id"])).size, 4);
  for (const { task, body } of bodies) {
    const serialized = JSON.stringify(body["messages"]);
    assert.match(serialized, new RegExp(task));
    assert.doesNotMatch(serialized, new RegExp(task === "task-A" ? "task-B" : "task-A"));
  }
});

test("opt-out, nonstream Responses and other protocols never use admission retries", async (t) => {
  for (const [url, payload, enabled] of [
    ["/v1/responses", { input: "x", stream: true }, false],
    ["/v1/responses", { input: "x", stream: false }, true],
    ["/v1/messages", { messages: [{ role: "user", content: "x" }], max_tokens: 10, stream: true }, true],
    ["/v1/chat/completions", { messages: [{ role: "user", content: "x" }], stream: true }, true],
  ] as const) {
    let attempts = 0;
    const { app } = fixture(async () => { attempts++; return new Response(full); }, {
      admission: { ...config.admission!, enabled },
    });
    t.after(() => app.close());
    await app.inject({
      method: "POST", url, headers: { authorization: "Bearer test" }, payload: { model: "m", ...payload },
    });
    assert.equal(attempts, 1, url);
  }
});

test("HTTP disconnect and app close cancel admission backoff", async () => {
  for (const stopServer of [false, true]) {
    let attempts = 0;
    const { app } = fixture(async () => { attempts++; return new Response(full); }, {
      admission: { ...config.admission!, initialDelayMs: 10000, maxDelayMs: 10000 },
    });
    const address = await app.listen({ port: 0, host: "127.0.0.1" });
    const abort = new AbortController();
    try {
      const response = await fetch(address + "/v1/responses", {
        method: "POST", headers: { authorization: "Bearer test", "content-type": "application/json" },
        body: JSON.stringify({ model: "m", input: "x", stream: true }), signal: abort.signal,
      });
      assert.equal(response.status, 200);
      const reader = response.body!.getReader();
      await reader.read();
      await delay(10);
      if (stopServer) {
        const closed = app.close();
        while (!(await reader.read()).done) { /* drain */ }
        await closed;
      } else {
        abort.abort();
        await reader.cancel().catch(() => {});
        await delay(10);
      }
      assert.equal(attempts, 1);
    } finally {
      abort.abort();
      await app.close();
    }
  }
});

test("streaming Responses keep in_progress while upstream headers hang", async () => {
  let release!: (value: Response) => void;
  const hang = new Promise<Response>((resolve) => { release = resolve; });
  let calls = 0;
  const { app } = fixture(async () => {
    calls++;
    return hang;
  }, {
    admission: { ...config.admission!, waitTimeoutMs: 60_000 },
  });
  const address = await app.listen({ port: 0, host: "127.0.0.1" });
  try {
    const response = await fetch(`${address}/v1/responses`, {
      method: "POST",
      headers: { authorization: "Bearer test", "content-type": "application/json" },
      body: JSON.stringify({ model: "m", input: "x", stream: true }),
    });
    assert.equal(response.status, 200);
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let text = "";
    const progress = (): number => (text.match(/event: response\.in_progress/g) ?? []).length;
    const deadline = Date.now() + 14_000;
    while (progress() < 2 && Date.now() < deadline) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
    }
    assert.ok(progress() >= 2, `expected heartbeat during header wait, got:\n${text}`);
    assert.equal(calls, 1);
    release(new Response(ok));
    while (!(await reader.read()).done) { /* drain */ }
  } finally {
    await app.close();
  }
});

test("deferred Responses use comments while upstream headers hang", async () => {
  let release!: (value: Response) => void;
  const hang = new Promise<Response>((resolve) => { release = resolve; });
  let calls = 0;
  const { app } = fixture(async () => {
    calls++;
    return hang;
  }, {
    responsesDeferLifecycle: true,
    admission: { ...config.admission!, waitTimeoutMs: 60_000 },
  });
  const address = await app.listen({ port: 0, host: "127.0.0.1" });
  try {
    const response = await fetch(`${address}/v1/responses`, {
      method: "POST",
      headers: { authorization: "Bearer test", "content-type": "application/json" },
      body: JSON.stringify({ model: "m", input: "x", stream: true }),
    });
    assert.equal(response.status, 200);
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    const first = decoder.decode((await reader.read()).value, { stream: true });
    assert.match(first, /: trae-admission /);
    assert.doesNotMatch(first, /event: response\.(?:created|in_progress)/);
    assert.equal(calls, 1);

    release(new Response(ok));
    let rest = "";
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      rest += decoder.decode(chunk.value, { stream: true });
    }
    assert.ok(rest.indexOf("event: response.created") < rest.indexOf("event: response.output_text.delta"));
  } finally {
    await app.close();
  }
});

test("oversized pending admission is rejected before contacting upstream", async (t) => {
  let calls = 0;
  const { app } = fixture(async () => { calls++; return new Response(ok); }, {
    admission: { ...config.admission!, maxBytes: 1 },
  });
  t.after(() => app.close());
  const result = await app.inject({
    method: "POST", url: "/v1/responses", headers: { authorization: "Bearer test" },
    payload: { input: "x", stream: true },
  });
  assert.equal(result.statusCode, 429);
  assert.equal(result.headers["retry-after"], "60");
  assert.equal(result.json().error.code, "rate_limit_exceeded");
  assert.equal(calls, 0);
});
