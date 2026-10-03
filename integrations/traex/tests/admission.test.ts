import { strict as assert } from "node:assert";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import { loadAdmissionConfig, loadResponsesDeferLifecycle, type AdmissionConfig } from "../src/config.js";
import { AdmissionPool, admissionDelay, admissionEventSource, retryAfterMs } from "../src/upstream/admission.js";
import { newStreamState } from "../src/upstream/engine.js";
import { responsesSseStream } from "../src/transform/responses-stream.js";

const config: AdmissionConfig = {
  enabled: true, initialDelayMs: 2, maxDelayMs: 4, waitTimeoutMs: 2000, maxRequests: 8, maxBytes: 10000,
};
const frame = (event: string, data: unknown): string => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const full = frame("error", { code: 4050, message: "queue full" });
const completed = frame("output", { response: "OK" }) + frame("done", { finish_reason: "stop" });
const sse = (text: string): Response => new Response(text, { headers: { "content-type": "text/event-stream" } });
const parse = (raw: string) => raw.split("\n\n").flatMap((block) => {
  const data = /^data: (.*)$/m.exec(block)?.[1];
  return data ? [JSON.parse(data)] : [];
});

function setup(open: (signal: AbortSignal) => Promise<Response>, overrides: Partial<AdmissionConfig> = {}) {
  const cfg = { ...config, ...overrides };
  const pool = new AdmissionPool(cfg);
  const lease = pool.reserve("account/model", 10);
  const abort = new AbortController();
  const phases: Array<{ phase: string; attempt: unknown }> = [];
  const source = admissionEventSource({
    config: cfg, pool, lease, open, idleMs: 50,
    log: (phase, fields) => phases.push({ phase, attempt: fields.attempt }),
  });
  const stream = responsesSseStream({
    eventSource: source, requestedModel: "m", toolMeta: new Map(), idleMs: 50, signal: abort.signal,
  });
  return { pool, abort, stream, phases, result: async () => parse(await new Response(stream).text()) };
}

test("admission retries eight explicit rejections within one response, without overlap", async () => {
  let attempts = 0;
  let previous: AbortSignal | undefined;
  const run = setup(async (signal) => {
    assert.ok(!previous || previous.aborted, "previous fetch must be closed");
    previous = signal;
    attempts++;
    return attempts <= 8 ? attempts % 2
      ? sse(full) : new Response(JSON.stringify({ code: "4050", message: "full" }), { status: 429 })
      : sse(completed);
  });
  const events = await run.result();
  assert.equal(attempts, 9);
  assert.equal(events.filter((event) => event.type === "response.created").length, 1);
  assert.equal(events.filter((event) => event.type === "response.failed").length, 0);
  assert.equal(events.at(-1).type, "response.completed");
  assert.deepEqual(events.map((event) => event.sequence_number), events.map((_, i) => i));
  assert.equal(new Set(events.filter((event) => event.response).map((event) => event.response.id)).size, 1);
  assert.equal(events.filter((event) => event.type === "response.output_text.delta").map((event) => event.delta).join(""), "OK");
  assert.equal(run.phases.filter((event) => event.phase === "admission_rejected").length, 8);
  assert.deepEqual(run.pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });
});

test("admission never replays after queue, output, usage, unknown activity or malformed data", async () => {
  const prefixes = [
    frame("queue_begin", { position: 10 }),
    frame("queue_end", {}),
    frame("output", { response: "partial" }),
    frame("output", { reasoning_content: "partial" }),
    frame("output", { tool_calls: [{ index: 0, function_call: { arguments: "{" } }] }),
    frame("output", {}),
    frame("token_usage", { prompt_tokens: 2 }),
    "event: token_usage\ndata: broken\n\n",
    frame("unknown-status", {}),
    "event: output\ndata: broken\n\n",
  ];
  for (const prefix of prefixes) {
    let attempts = 0;
    const run = setup(async () => { attempts++; return sse(prefix + full); });
    const events = await run.result();
    assert.equal(attempts, 1, prefix);
    assert.equal(events.at(-1).type, "response.failed", prefix);
    assert.deepEqual(run.pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });
  }
});

test("only explicit 4050 is retried, with typed terminal errors", async () => {
  const cases = [
    { response: () => sse(frame("error", { code: 4008 })), code: "insufficient_quota" },
    { response: () => sse(frame("error", { code: 4001 })), code: "invalid_prompt" },
    { response: () => sse(frame("error", { code: 4037 })), code: "invalid_prompt" },
    { response: () => sse(frame("error", { code: 9999, message: "queue full" })), code: "upstream_stream_error" },
    { response: () => sse(frame("error", { code: 4050, message: "prompt is too long" })), code: "invalid_prompt" },
    { response: () => new Response("{}", { status: 429 }), code: "rate_limit_exceeded" },
    { response: () => new Response('{"code":4050}', { status: 401 }), code: "invalid_api_key" },
    { response: () => new Response('{"code":4050}', { status: 403 }), code: "invalid_api_key" },
    { response: () => sse(""), code: "upstream_stream_incomplete" },
    { response: () => { throw new Error("network failed"); }, code: "upstream_stream_read_error" },
  ];
  for (const item of cases) {
    let attempts = 0;
    const run = setup(async () => { attempts++; return item.response(); });
    const events = await run.result();
    assert.equal(attempts, 1);
    assert.equal(events.at(-1).response.error.code, item.code);
  }
});

test("admission budget ends local waiting but never kills an admitted stream", async () => {
  const run = setup(async () => sse(full), { waitTimeoutMs: 25, initialDelayMs: 100, maxDelayMs: 100 });
  const events = await run.result();
  assert.equal(events.at(-1).response.error.code, "rate_limit_exceeded");
  assert.match(events.at(-1).response.error.message, /budget exhausted/);
  let attempts = 0;
  const admitted = setup(async () => {
    attempts++;
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame("queue_begin", { position: 1 })));
        setTimeout(() => {
          controller.enqueue(new TextEncoder().encode(completed));
          controller.close();
        }, 30);
      },
    }));
  }, { waitTimeoutMs: 10 });
  assert.equal((await admitted.result()).at(-1).type, "response.completed");
  assert.equal(attempts, 1);
});

test("cancel during backoff or pending fetch prevents any further attempt", async () => {
  let attempts = 0;
  const run = setup(async () => { attempts++; return sse(full); }, { initialDelayMs: 100, maxDelayMs: 100 });
  const result = run.result();
  await delay(10);
  run.abort.abort();
  const events = await result;
  assert.equal(attempts, 1);
  assert.ok(!events.some((event) => event.type === "response.failed" || event.type === "response.completed"));
  assert.deepEqual(run.pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });

  let fetchAborted = false;
  const pending = setup(async (signal) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => { fetchAborted = true; reject(signal.reason); }, { once: true });
  }));
  const pendingResult = pending.result();
  await delay(5);
  pending.abort.abort();
  await pendingResult;
  assert.equal(fetchAborted, true);
  assert.equal(pending.pool.snapshot().requests, 0);
});

test("direct downstream cancel and service shutdown cancel open upstream readers", async () => {
  for (const shutdown of [false, true]) {
    let canceled = false;
    const run = setup(async () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode(frame("queue_begin", { position: 4 }))); },
      cancel() { canceled = true; },
    })));
    const reader = run.stream.getReader();
    await reader.read();
    await delay(5);
    if (shutdown) run.pool.close();
    else await reader.cancel();
    await delay(5);
    assert.equal(canceled, true);
    if (shutdown) while (!(await reader.read()).done) { /* drain */ }
    assert.equal(run.pool.snapshot().requests, 0);
  }
});

test("scheduler serializes per principal/model, is FIFO, and accounts for bytes", async () => {
  const pool = new AdmissionPool({ ...config, maxRequests: 3, maxBytes: 30 });
  const a = pool.reserve("a/model", 10);
  const b = pool.reserve("a/model", 10);
  const c = pool.reserve("b/model", 10);
  assert.throws(() => pool.reserve("other", 1), /capacity/);
  const signal = new AbortController().signal;
  await a.acquire(signal);
  await c.acquire(signal);
  const order: string[] = [];
  const nextB = b.acquire(signal).then(() => order.push("b"));
  a.defer(15);
  const nextA = a.acquire(signal).then(() => order.push("a"));
  await delay(3);
  assert.deepEqual(order, []);
  await nextB;
  b.release();
  await nextA;
  assert.deepEqual(order, ["b", "a"]);
  a.release();
  c.release();
  assert.deepEqual(pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });
  assert.throws(() => pool.reserve("huge", 31), /capacity/);
});

test("retry-after is a lower bound and error inspection is bounded", async () => {
  const times: number[] = [];
  const run = setup(async () => {
    times.push(performance.now());
    return times.length === 1 ? new Response('{"code":4050}', { status: 429, headers: { "retry-after": "0.03" } })
      : sse(completed);
  });
  await run.result();
  assert.ok(times[1]! - times[0]! >= 28);
  let attempts = 0;
  const huge = setup(async () => { attempts++; return new Response("x".repeat(65537), { status: 429 }); });
  assert.equal((await huge.result()).at(-1).type, "response.failed");
  assert.equal(attempts, 1);
});

test("six-hour budget and local heartbeat use no upstream queue position", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const cfg = { ...config, waitTimeoutMs: 21600000, initialDelayMs: 30000, maxDelayMs: 120000 };
  const pool = new AdmissionPool(cfg);
  const progress: unknown[] = [];
  const state = newStreamState();
  const source = admissionEventSource({
    config: cfg, pool, lease: pool.reserve("a", 1), idleMs: 5000, open: async () => sse(full),
  });
  const draining = (async () => {
    for await (const _ of source(state, new AbortController().signal, undefined, (p) => progress.push(p))) void _;
  })();
  const rejected = assert.rejects(draining, /budget exhausted/);
  for (let i = 0; i < 40; i++) await Promise.resolve();
  now = 10000;
  t.mock.timers.tick(10000);
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.ok(progress.length > 0);
  assert.ok(progress.every((p) => !("position" in (p as object))));
  now = 21600000;
  t.mock.timers.tick(21590000);
  await rejected;
  assert.equal(pool.snapshot().requests, 0);
});

test("Responses lifecycle deferral configuration is strict and disabled by default", () => {
  assert.equal(loadResponsesDeferLifecycle({}), false);
  assert.equal(loadResponsesDeferLifecycle({ TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE: "true" }), true);
  assert.equal(loadResponsesDeferLifecycle({ TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE: "1" }), true);
  assert.equal(loadResponsesDeferLifecycle({ TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE: "false" }), false);
  assert.equal(loadResponsesDeferLifecycle({ TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE: "0" }), false);
  assert.throws(
    () => loadResponsesDeferLifecycle({ TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE: "yes" }),
    /must be true\/false or 1\/0/,
  );
});

test("configuration validates defaults, opt-out, durations and limits", () => {
  assert.deepEqual(loadAdmissionConfig({}), {
    enabled: true, initialDelayMs: 30000, maxDelayMs: 120000, waitTimeoutMs: 21600000,
    maxRequests: 8, maxBytes: 1073741824,
  });
  assert.equal(loadAdmissionConfig({ TRAEX_BRIDGE_ADMISSION_ENABLED: "false" }).enabled, false);
  assert.equal(loadAdmissionConfig({ TRAEX_BRIDGE_ADMISSION_WAIT_TIMEOUT: "0" }).waitTimeoutMs, 0);
  for (const env of [
    { TRAEX_BRIDGE_ADMISSION_ENABLED: "yes" },
    { TRAEX_BRIDGE_ADMISSION_MAX_REQUESTS: "0" },
    { TRAEX_BRIDGE_ADMISSION_MAX_BYTES: "NaN" },
    { TRAEX_BRIDGE_ADMISSION_INITIAL_DELAY_MS: "-1" },
    { TRAEX_BRIDGE_ADMISSION_WAIT_TIMEOUT: "1000000h" },
    { TRAEX_BRIDGE_ADMISSION_MAX_DELAY: "1s" },
  ]) assert.throws(() => loadAdmissionConfig(env));
  const cfg = loadAdmissionConfig({});
  assert.equal(admissionDelay(cfg, 1, 0.5), 30000);
  assert.equal(admissionDelay(cfg, 2, 0.5), 60000);
  assert.equal(admissionDelay(cfg, 9999, 1), 120000);
  assert.equal(retryAfterMs("120"), 120000);
  assert.equal(retryAfterMs("Wed, 09 Sep 2026 00:01:00 GMT", Date.parse("2026-09-09T00:00:00Z")), 60000);
  assert.equal(retryAfterMs("bad"), 0);
});

test("admission budget cancels a pending header wait and stalled upstream is not retried", async () => {
  let canceled = false;
  const pending = setup(async (signal) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => { canceled = true; reject(signal.reason); }, { once: true });
  }), { waitTimeoutMs: 20 });
  assert.equal((await pending.result()).at(-1).response.error.code, "rate_limit_exceeded");
  assert.equal(canceled, true);

  let attempts = 0;
  let readerCanceled = false;
  const stalled = setup(async () => {
    attempts++;
    return new Response(new ReadableStream({ cancel() { readerCanceled = true; } }));
  });
  const [events] = await Promise.all([stalled.result(), delay(80)]);
  assert.match(events.at(-1).response.error.message, /idle timeout/);
  assert.equal(attempts, 1);
  assert.equal(readerCanceled, true);
  assert.equal(stalled.pool.snapshot().requests, 0);
});

test("pre-aborted requests never submit and canceled FIFO waiters do not block successors", async () => {
  const pool = new AdmissionPool(config);
  const abort = new AbortController();
  abort.abort();
  let attempts = 0;
  const state = newStreamState();
  const source = admissionEventSource({
    config, pool, lease: pool.reserve("a", 1), idleMs: 50,
    open: async () => { attempts++; return sse(completed); },
  });
  await assert.rejects(async () => {
    for await (const _ of source(state, abort.signal, undefined, () => {})) void _;
  });
  assert.equal(attempts, 0);
  assert.equal(pool.snapshot().requests, 0);
  const a = pool.reserve("a", 1);
  const b = pool.reserve("a", 1);
  const c = pool.reserve("a", 1);
  const live = new AbortController();
  const stop = new AbortController();
  await a.acquire(live.signal);
  const waiting = assert.rejects(b.acquire(stop.signal));
  const next = c.acquire(live.signal);
  stop.abort();
  await waiting;
  b.release();
  a.release();
  await next;
  c.release();
  assert.deepEqual(pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });
});

test("abort during listener registration rejects immediately and leaves no queued waiter", async () => {
  const pool = new AdmissionPool(config);
  const active = pool.reserve("a", 1);
  await active.acquire(new AbortController().signal);

  const raced = pool.reserve("a", 1);
  const abort = new AbortController();
  const signal = abort.signal;
  const addEventListener = signal.addEventListener.bind(signal);
  Object.defineProperty(signal, "addEventListener", {
    value(type: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions) {
      abort.abort(new Error("aborted during listener registration"));
      addEventListener(type, listener, options);
    },
  });
  await assert.rejects(
    Promise.race([
      raced.acquire(signal),
      delay(50).then(() => { throw new Error("acquire remained pending"); }),
    ]),
    /aborted during listener registration/,
  );
  raced.release();
  active.release();

  const next = pool.reserve("a", 1);
  await next.acquire(new AbortController().signal);
  next.release();
  assert.deepEqual(pool.snapshot(), { requests: 0, bytes: 0, groups: 0 });
});

test("header waits keep the local heartbeat, but a stalled SSE is not retried", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const cfg = { ...config, waitTimeoutMs: 60000 };
  const pool = new AdmissionPool(cfg);
  const state = newStreamState();
  const abort = new AbortController();
  let openStarted = false;
  let ticks = 0;
  const source = admissionEventSource({
    config: cfg, pool, lease: pool.reserve("a", 1), idleMs: 5000,
    open: async (signal) => {
      openStarted = true;
      return new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    },
  });
  const drained = assert.rejects(async () => {
    for await (const _ of source(state, abort.signal, undefined, () => { ticks++; })) void _;
  });
  for (let i = 0; i < 40; i++) await Promise.resolve();
  assert.equal(openStarted, true);
  t.mock.timers.tick(20000);
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.ok(ticks >= 1, "downstream must stay alive while upstream headers are pending");
  abort.abort();
  await drained;
});

test("retried 4050 does not record an error event", async () => {
  let errorEvents = 0;
  let attempts = 0;
  const cfg = { ...config, initialDelayMs: 1, maxDelayMs: 1 };
  const pool = new AdmissionPool(cfg);
  const state = newStreamState();
  const source = admissionEventSource({
    config: cfg, pool, lease: pool.reserve("a", 1), idleMs: 50,
    open: async () => {
      attempts++;
      return attempts <= 2
        ? attempts === 1
          ? sse(full)
          : new Response(JSON.stringify({ code: "4050", message: "full" }), { status: 429 })
        : sse(completed);
    },
  });
  for await (const _ of source(state, new AbortController().signal, {
    onErrorEvent: () => { errorEvents++; },
  }, () => {})) void _;
  assert.equal(attempts, 3);
  assert.equal(errorEvents, 0);
  assert.equal(state.sawTerminal, true);

  errorEvents = 0;
  attempts = 0;
  const quota = admissionEventSource({
    config: cfg, pool, lease: pool.reserve("b", 1), idleMs: 50,
    open: async () => {
      attempts++;
      return sse(frame("error", { code: 4008 }));
    },
  });
  const quotaState = newStreamState();
  for await (const _ of quota(quotaState, new AbortController().signal, {
    onErrorEvent: () => { errorEvents++; },
  }, () => {})) void _;
  assert.equal(attempts, 1);
  assert.equal(errorEvents, 1);
});

test("short backoffs do not repeatedly postpone the local heartbeat deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  let now = 0;
  t.mock.method(performance, "now", () => now);
  t.mock.method(Math, "random", () => 0.5);
  const cfg = { ...config, waitTimeoutMs: 0, initialDelayMs: 2000, maxDelayMs: 2000 };
  const pool = new AdmissionPool(cfg);
  let attempts = 0;
  let heartbeats = 0;
  const state = newStreamState();
  const source = admissionEventSource({
    config: cfg, pool, lease: pool.reserve("a", 1), idleMs: 5000,
    open: async () => { attempts++; return sse(attempts <= 8 ? full : completed); },
  });
  const draining = (async () => {
    for await (const _ of source(state, new AbortController().signal, undefined, () => { heartbeats++; })) void _;
  })();
  for (let step = 0; step < 9; step++) {
    for (let i = 0; i < 50; i++) await Promise.resolve();
    now += 2000;
    t.mock.timers.tick(2000);
  }
  await draining;
  assert.equal(attempts, 9);
  assert.ok(heartbeats >= 1, "each 2-second wait must share the same 10-second heartbeat deadline");
  assert.equal(state.sawTerminal, true);
});
