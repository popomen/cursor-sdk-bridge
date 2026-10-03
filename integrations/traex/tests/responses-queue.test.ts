import { strict as assert } from "node:assert";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import { responsesSseStream, type ResponsesStreamOptions } from "../src/transform/responses-stream.js";
import { SseBlockDecoder } from "../src/upstream/sse.js";

const encoder = new TextEncoder();
const frame = (event: string, data: unknown): string =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const queue = (position = 42): string =>
  frame("request_wait_in_queue", { position, queue_id: "test-queue" });
const done = frame("done", { finish_reason: "stop" });

function streamOf(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

async function collect(
  upstream: string | ReadableStream<Uint8Array>,
  options: Partial<ResponsesStreamOptions> = {},
) {
  const body = responsesSseStream({
    upstreamBody: typeof upstream === "string" ? streamOf(upstream) : upstream,
    requestedModel: "test-model",
    toolMeta: new Map(),
    idleMs: 5000,
    ...options,
  });
  const raw = await new Response(body).text();
  const decoder = new SseBlockDecoder();
  const events = [...decoder.push(encoder.encode(raw)), ...decoder.finish()].map((event) => ({
    type: event.event,
    data: JSON.parse(event.data),
  }));
  return { raw, events };
}

test("Responses emits lifecycle events immediately by default", async () => {
  const { raw, events } = await collect(queue() + frame("output", { response: "first token" }) + done);
  assert.deepEqual(events.slice(0, 2).map((event) => event.type), ["response.created", "response.in_progress"]);
  assert.ok(raw.indexOf("event: response.created") < raw.indexOf(": trae-queue "));
});

test("Responses delays lifecycle events until real upstream output", async () => {
  let releaseOutput!: () => void;
  const outputReady = new Promise<void>((resolve) => { releaseOutput = resolve; });
  const upstream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(queue()));
      await outputReady;
      controller.enqueue(encoder.encode(frame("output", { response: "first token" }) + done));
      controller.close();
    },
  });
  const reader = responsesSseStream({
    upstreamBody: upstream,
    requestedModel: "test-model",
    toolMeta: new Map(),
    idleMs: 5000,
    deferLifecycle: true,
  }).getReader();

  const first = new TextDecoder().decode((await reader.read()).value);
  assert.match(first, /^: trae-queue /);
  assert.doesNotMatch(first, /response\.created|response\.in_progress/);

  releaseOutput();
  let rest = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    rest += new TextDecoder().decode(chunk.value);
  }
  assert.ok(rest.indexOf("event: response.created") < rest.indexOf("event: response.output_text.delta"));
});

test("Responses queue keepalives are throttled, sequenced, and contain no synthetic output", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const times = [0, 9999, 10000, 10001, 20000];
  const { raw, events } = await collect(
    times.map(() => queue()).join("") + frame("output", { response: "real answer" }) + done,
    { hooks: { onQueue: () => { now = times.shift()!; } } },
  );

  assert.equal(raw.split(": trae-queue ").length - 1, 5);
  const progress = events.filter((event) => event.type === "response.in_progress");
  assert.equal(progress.length, 3, "initial status plus two 10-second keepalives");
  assert.equal(events.filter((event) => event.type === "response.created").length, 1);
  const created = events[0]!.data.response;
  for (const event of progress) {
    assert.deepEqual(event.data.response, created);
    assert.deepEqual(event.data.response.output, []);
  }
  assert.deepEqual(events.map((event) => event.data.sequence_number), events.map((_, i) => i));
  assert.deepEqual(
    events.filter((event) => event.type === "response.output_text.delta").map((event) => event.data.delta),
    ["real answer"],
  );
  assert.equal(events.at(-1)!.type, "response.completed");
  assert.equal(events.at(-1)!.data.response.id, created.id);
  assert.equal(events.at(-1)!.data.response.usage.output_tokens, 0);
});

test("deferred Responses keepalives stay comments until real output", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const times = [0, 9999, 10000, 10001, 20000];
  const { raw, events } = await collect(
    times.map(() => queue()).join("") + frame("output", { response: "real answer" }) + done,
    { deferLifecycle: true, hooks: { onQueue: () => { now = times.shift()!; } } },
  );

  assert.equal(raw.split(": trae-queue ").length - 1, 5);
  const progress = events.filter((event) => event.type === "response.in_progress");
  assert.equal(progress.length, 1, "pre-output queue activity must not emit parsed SSE events");
  assert.ok(raw.lastIndexOf(": trae-queue ") < raw.indexOf("event: response.created"));
  assert.deepEqual(events.slice(0, 3).map((event) => event.type), [
    "response.created", "response.in_progress", "response.output_item.added",
  ]);
});

test("Responses queue remains on one response across six simulated hours", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const count = 6 * 60 * 6;
  const { events } = await collect(queue().repeat(count) + frame("output", { response: "ok" }) + done, {
    hooks: { onQueue: () => { now += 10000; } },
  });
  assert.equal(events.filter((event) => event.type === "response.in_progress").length, count + 1);
  assert.equal(new Set(events.filter((event) => event.data.response).map((event) => event.data.response.id)).size, 1);
  assert.equal(events.at(-1)!.type, "response.completed");
});

test("Responses queue status preserves output order and does not expose unvalidated tools", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const { events } = await collect(
    frame("output", { response: "partial" }) +
    frame("output", {
      reasoning_content: "reasoning",
      tool_calls: [{ index: 0, id: "call-test", function_call: { name: "lookup", arguments: "{}" } }],
    }) +
    queue() + queue() + frame("done", { finish_reason: "tool_calls" }),
    { hooks: { onQueue: () => { now += 5000; } } },
  );
  const progress = events.filter((event) => event.type === "response.in_progress");
  assert.equal(progress.length, 2);
  const items = progress[1]!.data.response.output;
  assert.deepEqual(items.map((item: { type: string }) => item.type), ["message", "reasoning"]);
  assert.equal(items[0].content[0].text, "partial");
  assert.equal(items[1].summary[0].text, "reasoning");
  const added = events.filter((event) => event.type === "response.output_item.added");
  assert.deepEqual(items.map((item: { id: string }) => item.id), added.slice(0, 2).map((event) => event.data.item.id));
  assert.equal(added[2]!.data.item.type, "function_call");
  assert.ok(events.indexOf(added[2]!) > events.indexOf(progress[1]!));
  assert.equal(events.at(-1)!.type, "response.completed");
  assert.equal(events.at(-1)!.data.response.output[2].arguments, "{}");
});

test("Responses queue keepalives stop at completion, errors, malformed output, and EOF", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const cases = [
    { tail: frame("output", { response: "ok" }) + done + queue(), terminal: "response.completed" },
    { tail: frame("error", { code: 4050, message: "queue full" }) + queue(), terminal: "response.failed" },
    { tail: "event: output\ndata: invalid-json\n\n" + queue(), terminal: "response.failed" },
    { tail: "", terminal: "response.failed" },
  ];
  for (const item of cases) {
    const { events } = await collect(queue() + item.tail, {
      hooks: { onQueue: () => { now += 10000; } },
    });
    assert.equal(events.filter((event) => event.type === "response.in_progress").length, 2);
    assert.equal(events.at(-1)!.type, item.terminal);
    assert.equal(
      events.filter((event) => ["response.completed", "response.failed"].includes(event.type!)).length,
      1,
    );
  }
});

test("Responses queue cancellation stops keepalives and cancels the upstream reader", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const abort = new AbortController();
  let canceled = false;
  const outcomes: string[] = [];
  const upstream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode(queue())); },
    cancel() { canceled = true; },
  });
  const { events } = await collect(upstream, {
    signal: abort.signal,
    hooks: {
      onQueue: () => { now += 10000; abort.abort(); },
      onFinish: (outcome) => outcomes.push(outcome),
    },
  });
  assert.equal(canceled, true);
  assert.deepEqual(outcomes, ["downstream_canceled"]);
  assert.deepEqual(events.map((event) => event.type), ["response.created", "response.in_progress"]);
});

test("Responses keepalives do not conceal an upstream stall", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  let canceled = false;
  const upstream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode(queue())); },
    cancel() { canceled = true; },
  });
  // Keep the test process alive while the upstream reader's unref'ed idle timer fires.
  const [result] = await Promise.all([
    collect(upstream, { idleMs: 20, hooks: { onQueue: () => { now += 10000; } } }),
    delay(50),
  ]);
  assert.equal(canceled, true);
  assert.deepEqual(result.events.map((event) => event.type), [
    "response.created", "response.in_progress", "response.in_progress", "response.failed",
  ]);
});

test("Responses keepalive identity and sequence are scoped to each request", async (t) => {
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const options = { hooks: { onQueue: () => { now += 10000; } } };
  const first = await collect(queue() + frame("output", { response: "first" }) + done, options);
  const second = await collect(queue() + frame("output", { response: "second" }) + done, options);
  assert.notEqual(first.events[0]!.data.response.id, second.events[0]!.data.response.id);
  for (const { events } of [first, second]) {
    assert.deepEqual(events.map((event) => event.data.sequence_number), events.map((_, i) => i));
    assert.equal(new Set(events.filter((event) => event.data.response).map((event) => event.data.response.id)).size, 1);
  }
});
