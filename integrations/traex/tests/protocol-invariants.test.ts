/**
 * 协议结构不变量测试。
 *
 * 这里不测「输出等于某个 fixture」，而是测「输出满足协议的结构约束」——
 * fixture 会随实现细节漂移，不变量不会。每条都对应一个真实发生过的 bug。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { anthropicSseStream } from "../src/transform/anthropic-stream.js";
import { collectAnthropicMessage } from "../src/transform/anthropic-nonstream.js";
import { chatSseStream, collectChatCompletion } from "../src/transform/chat-stream.js";
import { anthropicToUnified } from "../src/transform/anthropic-request.js";
import { openaiChatToUnified } from "../src/transform/chat-request.js";
import { InvalidResponsesInputError, responsesToUnified } from "../src/transform/responses-request.js";
import { collectResponsesMessage, responsesSseStream } from "../src/transform/responses-stream.js";
import { buildUpstreamBody } from "../src/transform/upstream-body.js";
import { classifyOutcome, newStreamState, runUpstreamStream } from "../src/upstream/engine.js";

const enc = new TextEncoder();

function streamOf(s: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(c) {
      c.enqueue(enc.encode(s));
      c.close();
    },
  });
}

async function drain(rs: ReadableStream<Uint8Array>): Promise<string> {
  const r = rs.getReader();
  const dec = new TextDecoder();
  let out = "";
  for (;;) {
    const { done, value } = await r.read();
    if (done) break;
    out += dec.decode(value, { stream: true });
  }
  return out;
}

/** 解析 Anthropic SSE 成 (event, data) 序列 */
function parseSse(sse: string): Array<{ event: string; data: Record<string, unknown> }> {
  const out: Array<{ event: string; data: Record<string, unknown> }> = [];
  for (const block of sse.split("\n\n")) {
    const ev = /^event: (.+)$/m.exec(block)?.[1];
    const dt = /^data: (.+)$/m.exec(block)?.[1];
    if (ev && dt) out.push({ event: ev, data: JSON.parse(dt) as Record<string, unknown> });
  }
  return out;
}

const sse = (...frames: string[]): string => frames.join("");
const output = (o: Record<string, unknown>): string => `event: output\ndata: ${JSON.stringify(o)}\n\n`;
const done = (finish: string): string => `event: done\ndata: ${JSON.stringify({ finish_reason: finish })}\n\n`;

const anthropic = (upstream: string): Promise<string> =>
  drain(anthropicSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", stopSequences: [], idleMs: 5000,
  }));

// ---------------------------------------------------------------- engine

test("空 data 保活帧不杀流（WHATWG 合法，中间层常用）", async () => {
  const state = newStreamState();
  const upstream = sse(output({ response: "hi" }), "data:\n\n", done("stop"));
  for await (const _ of runUpstreamStream(streamOf(upstream), state, 5000)) void _;
  assert.equal(classifyOutcome(state), "completed");
});

test("未知 finish_reason 不当成正常完成", async () => {
  const state = newStreamState();
  const upstream = sse(output({ response: "x" }), done("tool_budget_exhausted"));
  for await (const _ of runUpstreamStream(streamOf(upstream), state, 5000)) void _;
  assert.equal(classifyOutcome(state), "upstream_unknown_finish");
});

test("已知的非空正常终止原因仍判为 completed", async () => {
  for (const fr of ["stop", "end_turn", "tool_calls"]) {
    const state = newStreamState();
    for await (const _ of runUpstreamStream(streamOf(sse(output({ response: "x" }), done(fr))), state, 5000)) void _;
    assert.equal(classifyOutcome(state), "completed", `finish_reason=${fr}`);
  }
});

test("空 finish_reason 只接受非空文本或结构完整工具调用", async () => {
  const outcome = async (frames: string): Promise<string> => {
    const state = newStreamState();
    for await (const _ of runUpstreamStream(streamOf(frames), state, 5000)) void _;
    return classifyOutcome(state);
  };
  assert.equal(await outcome(done("")), "upstream_unknown_finish");
  assert.equal(await outcome(sse(output({ response: "ok" }), done(""))), "completed");
  assert.equal(await outcome(sse(output({
    tool_calls: [{ index: 0, type: "function", function_call: { name: "lookup", arguments: "{}" } }],
  }), done(""))), "completed");
  assert.equal(await outcome(sse(output({
    tool_calls: [{ index: 0, type: "function", function_call: { name: "lookup", arguments: '{"id":' } }],
  }), done(""))), "upstream_unknown_finish");
  assert.equal(await outcome(sse(output({
    response: "partial text",
    tool_calls: [{ index: 0, type: "function", function_call: { name: "lookup", arguments: '{"id":' } }],
  }), done(""))), "completed");
  assert.equal(await outcome(sse(output({
    tool_calls: [{ index: 0, type: "function", function_call: { arguments: "{}" } }],
  }), done(""))), "upstream_unknown_finish");
  assert.equal(await outcome(sse(output({
    tool_calls: [{ index: 0, type: "custom", function_call: { name: "patch", arguments: "raw input" } }],
  }), done(""))), "completed");
});

test("Chat 与 Responses 请求都识别 structured-output schema", () => {
  const schema = { type: "object", properties: { answer: { type: "string" } } };
  const chat = openaiChatToUnified({
    messages: [{ role: "user", content: "answer" }],
    response_format: { type: "json_schema", json_schema: { name: "answer", schema } },
  }, "m");
  assert.equal(chat.structuredOutput, true);
  assert.equal(buildUpstreamBody(chat.request, { config: "cfg", model: "m" })["tool_choice"], "structured_output");

  const responses = responsesToUnified({
    input: "answer",
    text: { format: { type: "json_schema", name: "answer", schema } },
  }, "m");
  assert.equal(responses.structuredOutput, true);
  assert.equal(buildUpstreamBody(responses.request, { config: "cfg", model: "m" })["tool_choice"], "structured_output");
});

test("structured_output 调用在 Chat 与 Responses 的流式/非流式响应中还原为 JSON 文本", async () => {
  const json = '{"answer":"ok"}';
  const upstream = sse(output({
    tool_calls: [{
      index: 0,
      id: "call_structured",
      type: "function",
      function_call: { name: "structured_output", arguments: json },
    }],
  }), done("tool_calls"));

  const chatStream = await drain(chatSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", includeUsage: false, idleMs: 5000,
    structuredOutput: true,
  }));
  assert.match(chatStream, /"content":"\{\\"answer\\":\\"ok\\"\}"/);
  assert.doesNotMatch(chatStream, /"tool_calls"/);
  const chatNonstream = await collectChatCompletion(streamOf(upstream), "m", 5000, undefined, undefined, true);
  assert.equal(chatNonstream.ok, true);
  if (chatNonstream.ok) {
    const choice = (chatNonstream.body["choices"] as Array<Record<string, unknown>>)[0]!;
    const message = choice["message"] as Record<string, unknown>;
    assert.equal(message["content"], json);
    assert.equal(message["tool_calls"], undefined);
  }

  const responsesStream = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
    structuredOutput: true,
  })));
  assert.equal(responsesStream.some((event) => event.event.includes("function_call")), false);
  const completed = responsesStream.find((event) => event.event === "response.completed")!;
  const response = completed.data["response"] as Record<string, unknown>;
  const streamOutput = response["output"] as Array<Record<string, unknown>>;
  assert.deepEqual(streamOutput.map((item) => item["type"]), ["message"]);
  assert.equal(((streamOutput[0]!["content"] as Array<Record<string, unknown>>)[0]!)["text"], json);

  const responsesNonstream = await collectResponsesMessage(
    streamOf(upstream), "m", new Map(), 5000, undefined, undefined, true,
  );
  assert.equal(responsesNonstream.ok, true);
  if (responsesNonstream.ok) {
    const nonstreamOutput = responsesNonstream.body["output"] as Array<Record<string, unknown>>;
    assert.deepEqual(nonstreamOutput.map((item) => item["type"]), ["message"]);
    assert.equal(((nonstreamOutput[0]!["content"] as Array<Record<string, unknown>>)[0]!)["text"], json);
  }
});

test("当前请求的 queue/usage/finish hooks 在 engine 内统一触发", async () => {
  const calls: string[] = [];
  const upstream = sse(
    `event: request_wait_in_queue\ndata: {"position":908,"queue_id":"q1"}\n\n`,
    output({
      response: "hi",
      tool_calls: [{ index: 0, id: "call-hook", function_call: { name: "f", arguments: "{}" } }],
    }),
    `event: token_usage\ndata: {"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}\n\n`,
    done("stop"),
  );
  await drain(anthropicSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", stopSequences: [], idleMs: 5000,
    hooks: {
      onQueue: (q) => calls.push(`queue:${q.position}`),
      onUsage: (usage) => calls.push(`usage:${usage.total_tokens}`),
      onFinish: (outcome) => calls.push(`finish:${outcome}`),
    },
  }));
  assert.deepEqual(calls, ["queue:908", "usage:12", "finish:completed"]);
});

test("engine 为缺失的 tool id 生成请求内稳定值", async () => {
  const state = newStreamState();
  const seen: string[] = [];
  const upstream = sse(
    output({
      tool_calls: [{ index: 0, type: "function", function_call: { name: "lookup", arguments: '{"id":' } }],
    }),
    output({
      tool_calls: [{ index: 0, id: "late-upstream-id", function: { arguments: "1}" } }],
    }),
    done("tool_calls"),
  );

  for await (const ev of runUpstreamStream(streamOf(upstream), state, 5000)) {
    if (ev.type === "delta") seen.push(...ev.output.toolCalls.map((call) => call.id));
  }

  assert.equal(seen.length, 2);
  assert.match(seen[0]!, /^call_[0-9a-f]+$/);
  assert.deepEqual(seen, [seen[0], seen[0]]);
  assert.equal(state.toolCalls.get(0)!.id, seen[0]);
  assert.deepEqual(JSON.parse(state.toolCalls.get(0)!.arguments), { id: 1 });
});

test("已知终止码的 error 事件立即结束流，不再消费后续 output/done", async () => {
  const after = sse(
    output({ response: "should-not-appear" }),
    done("stop"),
  );
  const cases: Array<{ payload: string; expected: string }> = [
    { payload: '{"code":4008,"message":"usage limit used up"}', expected: "upstream_error_quota" },
    { payload: '{"code":4001,"message":"param is invalid"}', expected: "upstream_error_invalid_request" },
    { payload: '{"code":4037,"message":"model unavailable"}', expected: "upstream_error_invalid_request" },
    { payload: '{"code":4050,"message":"the request queue size is exceed"}', expected: "upstream_error_rate_limit" },
    { payload: '{"code":5001,"message":"prompt is too long"}', expected: "upstream_error_invalid_request" },
  ];

  for (const c of cases) {
    const state = newStreamState();
    const types: string[] = [];
    const upstream = sse(`event: error\ndata: ${c.payload}\n\n`, after);
    for await (const ev of runUpstreamStream(streamOf(upstream), state, 5000)) {
      types.push(ev.type);
    }
    assert.deepEqual(types, ["errorEvent"], c.payload);
    assert.equal(classifyOutcome(state), c.expected, c.payload);
    assert.equal(state.sawTerminal, false, c.payload);
  }
});

test("未知 error 码继续等待 terminal，三协议都把已知终止码写成明确错误", async () => {
  const unknownThenDone = sse(
    `event: error\ndata: {"code":5001,"message":"internal boom"}\n\n`,
    output({ response: "still-here" }),
    done("stop"),
  );
  const state = newStreamState();
  const types: string[] = [];
  for await (const ev of runUpstreamStream(streamOf(unknownThenDone), state, 5000)) {
    types.push(ev.type);
  }
  assert.deepEqual(types, ["errorEvent", "delta", "terminal"]);
  assert.equal(classifyOutcome(state), "upstream_error_event");
  assert.equal(state.sawTerminal, true);

  const terminal = (code: number, message: string): string =>
    `event: error\ndata: ${JSON.stringify({ code, message })}\n\n`;

  const anth = parseSse(await anthropic(terminal(4050, "the request queue size is exceed")));
  assert.ok(anth.some((f) => f.event === "error"), "Anthropic 流式应发 error 事件");
  assert.equal(anth.some((f) => f.event === "message_stop"), false, "Anthropic 终止码不能当成功收尾");

  const chat = await drain(chatSseStream({
    upstreamBody: streamOf(terminal(4008, "")), requestedModel: "m", includeUsage: false, idleMs: 5000,
  }));
  assert.ok(chat.includes('"error"'), "Chat 流式应明确报错");
  assert.ok(chat.includes("insufficient_quota") || chat.includes("rate_limit"), chat.slice(0, 400));

  const responses = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(terminal(4037, "")), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
  })));
  assert.ok(responses.some((f) => f.event === "response.failed"), "Responses 流式应发 response.failed");
  assert.equal(responses.some((f) => f.event === "response.completed"), false);

  const anthJson = await collectAnthropicMessage(streamOf(terminal(4008, "usage limit used up")), "m", 5000);
  assert.equal(anthJson.ok, false);
  if (!anthJson.ok) {
    assert.equal(anthJson.status, 429);
    assert.equal(anthJson.errorType, "rate_limit_error");
  }

  const chatJson = await collectChatCompletion(streamOf(terminal(4037, "")), "m", 5000);
  assert.equal(chatJson.ok, false);
  if (!chatJson.ok) {
    assert.equal(chatJson.status, 400);
    assert.equal(chatJson.errorType, "invalid_request_error");
  }

  const respJson = await collectResponsesMessage(streamOf(terminal(4050, "queue full")), "m", new Map(), 5000);
  assert.equal(respJson.ok, false);
  if (!respJson.ok) {
    assert.equal(respJson.status, 429);
    assert.equal(respJson.errorType, "rate_limit_error");
  }
});

test("engine 的 onFinish 对 queue 终态统一分类且只调用一次", async () => {
  const queued = `event: request_wait_in_queue\ndata: {"position":42,"queue_id":"q1"}\n\n`;
  const cases = [
    { label: "done", upstream: sse(queued, done("stop")), expected: "completed" },
    {
      label: "error",
      upstream: sse(queued, 'event: error\ndata: {"message":"usage limit used up"}\n\n', done("stop")),
      expected: "upstream_error_event",
    },
    { label: "eof", upstream: sse(queued), expected: "upstream_eof_without_terminal" },
  ];

  for (const c of cases) {
    const state = newStreamState();
    const outcomes: string[] = [];
    for await (const _ of runUpstreamStream(streamOf(c.upstream), state, 5000, undefined, {
      onFinish: (outcome) => outcomes.push(outcome),
    })) void _;
    assert.deepEqual(outcomes, [c.expected], c.label);
  }
});

// ------------------------------------------------- Anthropic 流式不变量

test("排队时下游能收到标准 ping 事件（注释行喂不饱 Claude Code 的看门狗）", async () => {
  // Claude Code 的流看门狗只认 SSE 事件；`: trae-queue` 注释被解析器吞掉，
  // 排队超 300s 就报 "Stream idle timeout - no chunks received" 掐流。
  const upstream = sse(
    `event: request_wait_in_queue\ndata: {"position":42,"queue_id":"q1"}\n\n`,
    `event: request_wait_in_queue\ndata: {"position":41,"queue_id":"q1"}\n\n`,
    output({ response: "hi" }),
    done("stop"),
  );
  const out = await anthropic(upstream);
  const pings = parseSse(out).filter((e) => e.event === "ping");
  assert.ok(pings.length >= 1, `排队时应发 ping 事件，实际输出：${out.slice(0, 400)}`);
  assert.deepEqual(pings[0]!.data, { type: "ping" }, "ping 事件必须是官方 {type:'ping'} 形状");
  assert.equal(pings.length, 1, "连续排队事件必须限频，不能每个都发 ping");
});

test("工具参数被正文打断也不丢失", async () => {
  // 模型边说话边调工具是常态。之前 tool 块被正文关掉后，
  // 后续参数片段进了缓冲区再也发不出去，客户端拿到半截 JSON。
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "exec", arguments: '{"cmd":' } }] }),
    output({ response: "let me run it" }),
    output({ tool_calls: [{ index: 0, function: { arguments: '"ls"}' } }] }),
    done("tool_calls"),
  );
  const frames = parseSse(await anthropic(upstream));
  const partials = frames
    .filter((f) => f.event === "content_block_delta")
    .map((f) => f.data["delta"] as Record<string, unknown>)
    .filter((d) => d["type"] === "input_json_delta")
    .map((d) => d["partial_json"] as string)
    .join("");
  assert.deepEqual(JSON.parse(partials), { cmd: "ls" });
});

test("content block 不交错：任何时刻最多一个块打开", async () => {
  const upstream = sse(
    output({ reasoning_content: "думаю" }),
    output({ response: "text" }),
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: "{}" } }] }),
    output({ response: "more text" }),
    done("tool_calls"),
  );
  const frames = parseSse(await anthropic(upstream));
  let open: number | null = null;
  for (const f of frames) {
    if (f.event === "content_block_start") {
      assert.equal(open, null, "上一个块还没关就开了新块");
      open = f.data["index"] as number;
    } else if (f.event === "content_block_stop") {
      assert.equal(open, f.data["index"], "stop 的 index 与当前打开的块不符");
      open = null;
    } else if (f.event === "content_block_delta") {
      assert.equal(open, f.data["index"], "delta 发给了没打开的块");
    }
  }
  assert.equal(open, null, "流结束时仍有块未封口");
});

test("只有 arguments、没有 name 的 tool delta 不能静默通过", async () => {
  // 首帧丢失时上游只发参数分片。既不能凭空造一个 tool_use，
  // 也不能宣称 stop_reason=tool_use 却不给块 —— 客户端会等一个永不到来的调用。
  const upstream = sse(
    output({ response: "working" }),
    output({ tool_calls: [{ index: 0, function: { arguments: '{"a":1}' } }] }),
    done("stop"),
  );
  const frames = parseSse(await anthropic(upstream));

  const hasError = frames.some((f) => f.event === "error");
  const hasToolBlock = frames.some(
    (f) => f.event === "content_block_start" &&
      (f.data["content_block"] as Record<string, unknown>)["type"] === "tool_use",
  );
  const delta = frames.find((f) => f.event === "message_delta")?.data["delta"] as
    | Record<string, unknown>
    | undefined;

  // 允许两种正确处理：明确报错，或者正常收尾但 stop_reason 不宣称 tool_use
  if (hasError) {
    assert.ok(!hasToolBlock, "报错的同时还发了 tool_use 块");
  } else {
    assert.ok(delta, "既没报错也没正常收尾");
    if (delta["stop_reason"] === "tool_use") {
      assert.ok(hasToolBlock, "宣称 tool_use 却没有 tool_use 块");
    }
  }
});

test("stop_reason=tool_use 时 content 里必须真的有 tool_use 块", async () => {
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: "{}" } }] }),
    done("tool_calls"),
  );
  const frames = parseSse(await anthropic(upstream));
  const delta = frames.find((f) => f.event === "message_delta")?.data["delta"] as Record<string, unknown>;
  const hasToolBlock = frames.some(
    (f) => f.event === "content_block_start" &&
      (f.data["content_block"] as Record<string, unknown>)["type"] === "tool_use",
  );
  assert.equal(delta["stop_reason"], "tool_use");
  assert.ok(hasToolBlock);
});

test("thinking 块封口前必须有 signature_delta", async () => {
  const upstream = sse(output({ reasoning_content: "思考" }), output({ response: "答案" }), done("stop"));
  const frames = parseSse(await anthropic(upstream));
  let inThinking = false;
  let signed = false;
  for (const f of frames) {
    if (f.event === "content_block_start") {
      inThinking = (f.data["content_block"] as Record<string, unknown>)["type"] === "thinking";
      signed = false;
    } else if (f.event === "content_block_delta" && inThinking) {
      if ((f.data["delta"] as Record<string, unknown>)["type"] === "signature_delta") signed = true;
    } else if (f.event === "content_block_stop" && inThinking) {
      assert.ok(signed, "thinking 块没签名就封口了");
      inThinking = false;
    }
  }
});

// ------------------------------------------------- 截断的工具调用

const TRUNCATED = sse(
  output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "write", arguments: '{"path":"/very/lo' } }] }),
  done("length"),
);

test("截断的工具调用不产生可执行的 tool_use（流式）", async () => {
  const frames = parseSse(await anthropic(TRUNCATED));
  const toolBlocks = frames.filter(
    (f) => f.event === "content_block_start" &&
      (f.data["content_block"] as Record<string, unknown>)["type"] === "tool_use",
  );
  assert.equal(toolBlocks.length, 0, "截断的参数被包装成了可执行的 tool_use");
});

test("截断的工具调用不产生可执行的 tool_use（非流式）", async () => {
  const res = await collectAnthropicMessage(streamOf(TRUNCATED), "m", 5000);
  assert.ok(res.ok);
  const content = res.body["content"] as Array<Record<string, unknown>>;
  assert.equal(content.filter((c) => c["type"] === "tool_use").length, 0);
  // 绝不能出现 {_raw: ...} 这种凭空捏造的参数
  assert.ok(!JSON.stringify(content).includes("_raw"));
});

test("正常终态下参数非法要判失败，而不是当成功返回", async () => {
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: '{"broken' } }] }),
    done("stop"),
  );
  const res = await collectAnthropicMessage(streamOf(upstream), "m", 5000);
  assert.equal(res.ok, false);

  const chat = await collectChatCompletion(streamOf(upstream), "m", 5000);
  assert.equal(chat.ok, false);

  const responses = await collectResponsesMessage(streamOf(upstream), "m", new Map(), 5000);
  assert.equal(responses.ok, false);

  const chatStream = await drain(chatSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", includeUsage: false, idleMs: 5000,
  }));
  assert.equal(chatStream.includes('"tool_calls"'), false, "非法工具不能先泄露进 Chat delta");
  assert.ok(chatStream.includes('"error"'), "Chat 流式应明确报错");

  const responsesStream = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
  })));
  assert.equal(
    responsesStream.some((frame) => frame.event === "response.output_item.added" &&
      (frame.data["item"] as Record<string, unknown>)["type"] === "function_call"),
    false,
    "非法工具不能先泄露进 Responses item",
  );
  assert.ok(responsesStream.some((frame) => frame.event === "response.failed"));
});

test("截断终态下 Chat/Responses 都不返回可执行工具调用", async () => {
  const chat = await collectChatCompletion(streamOf(TRUNCATED), "m", 5000);
  assert.ok(chat.ok);
  const chatMessage = (chat.body["choices"] as Array<Record<string, unknown>>)[0]!["message"] as Record<string, unknown>;
  assert.equal(chatMessage["tool_calls"], undefined);
  assert.match(String(chatMessage["content"]), /incomplete tool call/);

  const responses = await collectResponsesMessage(streamOf(TRUNCATED), "m", new Map(), 5000);
  assert.ok(responses.ok);
  const outputItems = responses.body["output"] as Array<Record<string, unknown>>;
  assert.equal(outputItems.some((item) => item["type"] === "function_call"), false);
  assert.ok(JSON.stringify(outputItems).includes("incomplete tool call"));
});

test("Anthropic 并行工具整批原子校验：后一个非法时前一个也不能先发出", async () => {
  const upstream = sse(
    output({ tool_calls: [
      { index: 0, id: "good", type: "function", function_call: { name: "safe", arguments: "{}" } },
      { index: 1, id: "bad", type: "function", function_call: { name: "broken", arguments: '{"x"' } },
    ] }),
    done("tool_calls"),
  );
  const frames = parseSse(await anthropic(upstream));
  assert.equal(frames.some((frame) => frame.event === "content_block_start" &&
    (frame.data["content_block"] as Record<string, unknown>)["type"] === "tool_use"), false);
  assert.ok(frames.some((frame) => frame.event === "error"));
});

test("Responses custom 工具在截断终态下流式/非流式都不可执行", async () => {
  const parsed = responsesToUnified({
    input: "run",
    tools: [{ type: "custom", name: "patch", description: "apply patch" }],
  }, "m");
  const upstream = sse(
    output({ tool_calls: [{
      index: 0,
      id: "c1",
      type: "function",
      function_call: { name: "patch", arguments: '{"input":"*** Begin' },
    }] }),
    done("length"),
  );
  const nonstream = await collectResponsesMessage(streamOf(upstream), "m", parsed.toolMeta, 5000);
  assert.ok(nonstream.ok);
  assert.equal(nonstream.body["status"], "incomplete");
  assert.equal((nonstream.body["output"] as Array<Record<string, unknown>>)
    .some((item) => item["type"] === "custom_tool_call"), false);

  const frames = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", toolMeta: parsed.toolMeta, idleMs: 5000,
  })));
  assert.equal(frames.some((frame) => frame.event === "response.output_item.added" &&
    (frame.data["item"] as Record<string, unknown>)["type"] === "custom_tool_call"), false);
  assert.ok(frames.some((frame) => frame.event === "response.incomplete"));
});

test("Responses custom 工具通过统一信封校验后发 input delta/done", async () => {
  const parsed = responsesToUnified({
    input: "run",
    tools: [{ type: "custom", name: "patch", description: "apply patch" }],
  }, "m");
  const upstream = sse(
    output({ tool_calls: [{
      index: 0,
      id: "c1",
      type: "function",
      function_call: { name: "patch", arguments: '{"input":"*** Begin Patch"}' },
    }] }),
    done("tool_calls"),
  );
  const frames = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", toolMeta: parsed.toolMeta, idleMs: 5000,
  })));
  const delta = frames.find((frame) => frame.event === "response.custom_tool_call_input.delta")!;
  const doneFrame = frames.find((frame) => frame.event === "response.custom_tool_call_input.done")!;
  assert.equal(delta.data["delta"], "*** Begin Patch");
  assert.equal(doneFrame.data["input"], "*** Begin Patch");
  assert.ok(frames.some((frame) => frame.event === "response.completed"));
});

test("Responses custom 工具允许以 input JSON 前缀开头的任意原始文本", async () => {
  const parsed = responsesToUnified({
    input: "run",
    tools: [{ type: "custom", name: "patch", description: "apply patch" }],
  }, "m");
  const raw = '{"input":"this is deliberately raw text with no closing envelope';
  const upstream = sse(
    output({ tool_calls: [{
      index: 0,
      id: "c-raw",
      type: "function",
      function_call: { name: "patch", arguments: raw },
    }] }),
    done("tool_calls"),
  );
  const response = await collectResponsesMessage(streamOf(upstream), "m", parsed.toolMeta, 5000);
  assert.ok(response.ok);
  const item = (response.body["output"] as Array<Record<string, unknown>>)
    .find((candidate) => candidate["type"] === "custom_tool_call");
  assert.equal(item?.["input"], raw);
});

test("Responses function_call 严格区分 fc_ item id 与 call_ call_id", async () => {
  const upstream = (): string => sse(
    output({ tool_calls: [{
      index: 0,
      id: "call_contract",
      type: "function",
      function_call: { name: "lookup", arguments: "{}" },
    }] }),
    done("tool_calls"),
  );

  const nonstream = await collectResponsesMessage(streamOf(upstream()), "m", new Map(), 5000);
  assert.ok(nonstream.ok);
  const item = (nonstream.body["output"] as Array<Record<string, unknown>>)
    .find((candidate) => candidate["type"] === "function_call")!;
  assert.match(String(item["id"]), /^fc_[0-9a-f]+$/);
  assert.equal(item["call_id"], "call_contract");
  assert.notEqual(item["id"], item["call_id"]);

  const frames = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream()), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
  })));
  const added = frames.find((frame) => frame.event === "response.output_item.added")!;
  const streamedItem = added.data["item"] as Record<string, unknown>;
  assert.match(String(streamedItem["id"]), /^fc_[0-9a-f]+$/);
  assert.equal(streamedItem["call_id"], "call_contract");
  const argsDone = frames.find((frame) => frame.event === "response.function_call_arguments.done")!;
  assert.equal(argsDone.data["item_id"], streamedItem["id"]);
});

test("合法的工具调用正常产出", async () => {
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: '{"a":1}' } }] }),
    done("tool_calls"),
  );
  const res = await collectAnthropicMessage(streamOf(upstream), "m", 5000);
  assert.ok(res.ok);
  const content = res.body["content"] as Array<Record<string, unknown>>;
  const tool = content.find((c) => c["type"] === "tool_use");
  assert.ok(tool);
  assert.deepEqual(tool["input"], { a: 1 });
  assert.equal(res.body["stop_reason"], "tool_use");
});

test("缺失上游 tool id 时，六条下游路径都暴露合法的请求内 synthetic id", async () => {
  const upstream = (): string => sse(
    output({
      tool_calls: [{ index: 0, type: "function", function_call: { name: "lookup", arguments: '{"id":' } }],
    }),
    output({ tool_calls: [{ index: 0, function: { arguments: "1}" } }] }),
    done("tool_calls"),
  );
  const isSyntheticId = (label: string, exposed: unknown): void => {
    assert.equal(typeof exposed, "string", `${label} 必须暴露 tool id`);
    assert.match(exposed as string, /^call_[0-9a-f]+$/, `${label} synthetic id 形状错误`);
  };

  const anthropicNonstream = await collectAnthropicMessage(
    streamOf(upstream()), "m", 5000,
  );
  assert.ok(anthropicNonstream.ok);
  const anthropicNonstreamTool = (anthropicNonstream.body["content"] as Array<Record<string, unknown>>)
    .find((item) => item["type"] === "tool_use");
  isSyntheticId("Anthropic nonstream", anthropicNonstreamTool?.["id"]);

  const anthropicStream = parseSse(await drain(anthropicSseStream({
    upstreamBody: streamOf(upstream()), requestedModel: "m", stopSequences: [], idleMs: 5000,
  })));
  const anthropicStreamTool = anthropicStream
    .find((frame) => frame.event === "content_block_start" &&
      (frame.data["content_block"] as Record<string, unknown>)["type"] === "tool_use")
    ?.data["content_block"] as Record<string, unknown> | undefined;
  isSyntheticId("Anthropic stream", anthropicStreamTool?.["id"]);

  const chatNonstream = await collectChatCompletion(
    streamOf(upstream()), "m", 5000,
  );
  assert.ok(chatNonstream.ok);
  const chatMessage = (chatNonstream.body["choices"] as Array<Record<string, unknown>>)[0]!["message"] as Record<string, unknown>;
  const chatNonstreamTool = (chatMessage["tool_calls"] as Array<Record<string, unknown>>)[0];
  isSyntheticId("Chat nonstream", chatNonstreamTool?.["id"]);

  const chatStream = await drain(chatSseStream({
    upstreamBody: streamOf(upstream()), requestedModel: "m", includeUsage: false, idleMs: 5000,
  }));
  const chatStreamTool = chatStream.split("\n")
    .filter((line) => line.startsWith("data: {") && !line.includes("[DONE]"))
    .map((line) => JSON.parse(line.slice(6)) as Record<string, unknown>)
    .flatMap((chunk) => chunk["choices"] as Array<Record<string, unknown>>)
    .map((choice) => choice["delta"] as Record<string, unknown>)
    .flatMap((delta) => (delta["tool_calls"] as Array<Record<string, unknown>> | undefined) ?? [])
    .find((tool) => typeof tool["id"] === "string");
  isSyntheticId("Chat stream", chatStreamTool?.["id"]);

  const responsesNonstream = await collectResponsesMessage(
    streamOf(upstream()), "m", new Map(), 5000,
  );
  assert.ok(responsesNonstream.ok);
  const responsesNonstreamTool = (responsesNonstream.body["output"] as Array<Record<string, unknown>>)
    .find((item) => item["type"] === "function_call");
  isSyntheticId("Responses nonstream", responsesNonstreamTool?.["call_id"]);

  const responsesStream = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream()), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
  })));
  const responsesStreamTool = responsesStream
    .find((frame) => frame.event === "response.output_item.added" &&
      (frame.data["item"] as Record<string, unknown>)["type"] === "function_call")
    ?.data["item"] as Record<string, unknown> | undefined;
  isSyntheticId("Responses stream", responsesStreamTool?.["call_id"]);
});

// ------------------------------------------------- 请求侧

test("Responses：namespace 工具始终用全限定上游名并可还原", async () => {
  const parsed = responsesToUnified({
    input: "call mcp__node_repl__js",
    tools: [{
      type: "namespace",
      name: "mcp__node_repl",
      tools: [{ type: "function", name: "js", parameters: { type: "object" } }],
    }],
  }, "m");
  const upstreamName = "mcp__node_repl__js";
  assert.equal(parsed.request.tools?.[0]?.function?.name, upstreamName);

  const response = await collectResponsesMessage(
    streamOf(sse(
      output({
        tool_calls: [{
          index: 0,
          id: "call-new",
          type: "function",
          function_call: { name: upstreamName, arguments: '{"code":"1+1"}' },
        }],
      }),
      done("tool_calls"),
    )),
    "m",
    parsed.toolMeta,
    5000,
  );
  assert.ok(response.ok);
  const item = (response.body["output"] as Array<Record<string, unknown>>)
    .find((candidate) => candidate["type"] === "function_call");
  assert.equal(item?.["namespace"], "mcp__node_repl");
  assert.equal(item?.["name"], "js");
});

test("Responses：并行工具调用合并成一条 assistant 消息", async () => {
  const r = responsesToUnified({
    model: "m",
    input: [
      { type: "function_call", call_id: "a", name: "f1", arguments: '{"x":1}' },
      { type: "function_call", call_id: "b", name: "f2", arguments: '{"y":2}' },
      { type: "function_call_output", call_id: "a", output: "ra" },
      { type: "function_call_output", call_id: "b", output: "rb" },
    ],
  }, "d");
  const roles = (r.request.messages ?? []).map((m) => m.role);
  assert.deepEqual(roles, ["assistant", "tool", "tool"]);
  assert.equal((r.request.messages?.[0]?.tool_calls ?? []).length, 2);
  // index 要按序递增
  assert.deepEqual((r.request.messages?.[0]?.tool_calls ?? []).map((t) => t.index), [0, 1]);
});

test("Responses：tool_search_call 的对象型 arguments 不丢失", async () => {
  const r = responsesToUnified({
    model: "m",
    input: [{ type: "tool_search_call", call_id: "ts", name: "tool_search", arguments: { query: "browser" } }],
  }, "d");
  const args = r.request.messages?.[0]?.tool_calls?.[0]?.function?.arguments;
  assert.deepEqual(JSON.parse(args as string), { query: "browser" });
});

test("Responses：tool_search_output 的工具列表要回放给模型", async () => {
  const r = responsesToUnified({
    model: "m",
    input: [{ type: "tool_search_output", call_id: "ts", tools: [{ type: "function", name: "found" }] }],
  }, "d");
  const text = (r.request.messages?.[0]?.content as Array<Record<string, unknown>>)[0]?.["text"] as string;
  assert.ok(text.includes("found"), "工具清单丢失，模型会反复重搜");
});

test("Responses：agent_message 的 encrypted_content 要保留，普通 message 丢弃", async () => {
  const kept = responsesToUnified({
    model: "m",
    input: [{ type: "agent_message", role: "user", content: [{ type: "encrypted_content", encrypted_content: "payload" }] }],
  }, "d");
  assert.equal((kept.request.messages ?? []).length, 1);

  const dropped = responsesToUnified({
    model: "m",
    input: [{ type: "message", role: "user", content: [{ type: "encrypted_content", encrypted_content: "payload" }] }],
  }, "d");
  assert.equal((dropped.request.messages ?? []).length, 0);
});

test("Responses：空工具输出用占位符而非空串", async () => {
  const r = responsesToUnified({
    model: "m",
    input: [{ type: "function_call_output", call_id: "a", output: "" }],
  }, "d");
  const text = (r.request.messages?.[0]?.content as Array<Record<string, unknown>>)[0]?.["text"] as string;
  assert.notEqual(text, "");
});

test("Responses：named standalone function output 不把 fco item id 当成 call_id", () => {
  const id = "fco_01a08f52-fe5d-7242-b5e0-53650510fd72";
  const text = responsesToUnified({
    model: "m",
    input: [{ type: "function_call_output", id, name: "heartbeat", output: "still running" }],
  }, "d").request.messages ?? [];
  assert.deepEqual(text.map((message) => message.role), ["user"]);
  assert.equal(text[0]!.tool_call_id, undefined);
  assert.equal((text[0]!.content as Array<Record<string, unknown>>)[0]?.["text"], "still running");
  assert.doesNotMatch(JSON.stringify(text), new RegExp(id));

  const empty = responsesToUnified({
    model: "m",
    input: [{ type: "function_call_output", id: "fco_empty", name: "heartbeat", output: "" }],
  }, "d").request.messages ?? [];
  assert.equal(empty[0]!.role, "user");
  assert.notEqual((empty[0]!.content as Array<Record<string, unknown>>)[0]?.["text"], "");

  const media = responsesToUnified({
    model: "m",
    input: [{
      type: "function_call_output",
      id: "fco_media",
      name: "heartbeat",
      output: [{ type: "input_image", image_url: "data:image/png;base64,AAAA" }],
    }],
  }, "d").request.messages ?? [];
  assert.equal(media[0]!.role, "user");
  assert.equal((media[0]!.content as Array<Record<string, unknown>>)[0]?.["text"], "[media attached below]");
  assert.match(JSON.stringify(media[0]!.content), /data:image\/png;base64,AAAA/);
});

test("Responses：标准 function output 与无名旧形态仍保持 tool 语义", () => {
  const standard = responsesToUnified({
    model: "m",
    input: [{ type: "function_call_output", id: "fco_item", call_id: "call_real", name: "lookup", output: "ok" }],
  }, "d").request.messages ?? [];
  assert.equal(standard[0]!.role, "tool");
  assert.equal(standard[0]!.tool_call_id, "call_real");

  const legacy = responsesToUnified({
    model: "m",
    input: [{ type: "function_call_output", id: "call_legacy", output: "ok" }],
  }, "d").request.messages ?? [];
  assert.equal(legacy[0]!.role, "tool");
  assert.equal(legacy[0]!.tool_call_id, "call_legacy");
});

test("Anthropic：只有 assistant 轮的 tool_use 才转成 tool_calls", async () => {
  const evil = anthropicToUnified({
    model: "m",
    messages: [{ role: "user", content: [{ type: "tool_use", id: "x", name: "evil", input: {} }] }],
  }, "d");
  assert.equal(evil.request.messages?.[0]?.tool_calls, undefined);

  const ok = anthropicToUnified({
    model: "m",
    messages: [{ role: "assistant", content: [{ type: "tool_use", id: "x", name: "f", input: {} }] }],
  }, "d");
  assert.equal((ok.request.messages?.[0]?.tool_calls ?? []).length, 1);
});

test("Anthropic：tool_result 里的图片不丢弃，抽成紧随的 user 图片消息", () => {
  // Claude Code 用 Read 读图片时，图片在 tool_result 的 image block 里回传。
  // 曾经这里只取 text 块，图片被静默丢弃 → 上游拿到空 output 直接
  // 4027 "No tool output found"，且一旦进入会话历史，之后每个请求都带毒。
  const r = anthropicToUnified({
    model: "m",
    messages: [
      { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "Read", input: {} }] },
      {
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: "t1",
          content: [{ type: "image", source: { type: "base64", media_type: "image/png", data: "AAAA" } }],
        }],
      },
    ],
  }, "d");

  const msgs = r.request.messages ?? [];
  const toolIdx = msgs.findIndex((m) => m.role === "tool");
  assert.ok(toolIdx >= 0, "应有 role:tool 消息");
  const toolText = (msgs[toolIdx]!.content as Array<Record<string, unknown>>)[0]!["text"] as string;
  assert.notEqual(toolText.trim(), "", "tool 消息的 output 不能为空（上游 4027）");

  const follow = msgs[toolIdx + 1];
  assert.equal(follow?.role, "user", "图片应以 user 消息紧随 tool 消息");
  const followJson = JSON.stringify(follow?.content);
  assert.ok(followJson.includes("data:image/png;base64,AAAA"), `图片 part 丢失: ${followJson}`);
});

test("Anthropic：tool_result 的 text 与图片共存时两者都保留", () => {
  const r = anthropicToUnified({
    model: "m",
    messages: [
      { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "Read", input: {} }] },
      {
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: "t1",
          content: [
            { type: "text", text: "file meta: 400x200" },
            { type: "image", source: { type: "base64", media_type: "image/jpeg", data: "BBBB" } },
          ],
        }],
      },
    ],
  }, "d");

  const msgs = r.request.messages ?? [];
  const toolMsg = msgs.find((m) => m.role === "tool");
  assert.ok(JSON.stringify(toolMsg?.content).includes("file meta: 400x200"), "text 部分应留在 tool 消息");
  assert.ok(JSON.stringify(msgs).includes("data:image/jpeg;base64,BBBB"), "image 部分不能丢");
});

test("Anthropic：并行工具调用含图片时，多条 tool 消息必须连续，图片延后", () => {
  // Claude Code 并行调用工具（如同时 Read 两张图）时，一条 user 消息里带多个
  // tool_result。若在 tool 消息之间插入 user 图片消息，就打断了「紧跟 assistant
  // 的连续 tool 段」，上游按 index 复原 tool_result 时后面的 tool_use 找不到
  // 紧邻的 tool_result → Bedrock 400（tool_use ids ... without tool_result
  // blocks immediately after），且毒害会话历史导致每次重试都失败。
  const r = anthropicToUnified({
    model: "m",
    messages: [
      {
        role: "assistant",
        content: [
          { type: "tool_use", id: "t1", name: "Read", input: {} },
          { type: "tool_use", id: "t2", name: "Read", input: {} },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            content: [{ type: "image", source: { type: "base64", media_type: "image/png", data: "AAAA" } }],
          },
          {
            type: "tool_result",
            tool_use_id: "t2",
            content: [{ type: "image", source: { type: "base64", media_type: "image/png", data: "CCCC" } }],
          },
        ],
      },
    ],
  }, "d");

  const msgs = r.request.messages ?? [];
  const firstTool = msgs.findIndex((m) => m.role === "tool");
  assert.ok(firstTool >= 0, "应有 role:tool 消息");
  // 两条 tool 消息必须相邻，中间不能插 user
  assert.equal(msgs[firstTool]!.role, "tool", "第一条应为 tool");
  assert.equal(msgs[firstTool + 1]!.role, "tool", "第二条紧接着也必须是 tool，不能被 user 打断");
  assert.equal(msgs[firstTool + 1]!.tool_call_id, "t2", "第二条 tool 对应 t2");
  // 图片延后到最后一条 user 消息，且两张图都在
  const follow = msgs[firstTool + 2];
  assert.equal(follow?.role, "user", "图片应以单条 user 消息紧随连续 tool 段");
  const followJson = JSON.stringify(follow?.content);
  assert.ok(followJson.includes("data:image/png;base64,AAAA"), "第一张图应保留");
  assert.ok(followJson.includes("data:image/png;base64,CCCC"), "第二张图应保留");
  assert.ok(followJson.indexOf("tool result t1") < followJson.indexOf("data:image/png;base64,AAAA"));
  assert.ok(followJson.indexOf("tool result t2") < followJson.indexOf("data:image/png;base64,CCCC"));
});

test("Anthropic：Claude Code 的 context-1m beta 保留大上下文路由", () => {
  const parsed = anthropicToUnified(
    { model: "openrouter-3o", messages: [{ role: "user", content: "hi" }] },
    "d",
    "prompt-caching-2024-07-31,context-1m-2025-08-07",
  );

  assert.equal(parsed.requestedModel, "openrouter-3o", "响应仍应回显客户端 body.model");
  assert.equal(parsed.request.model, "openrouter-3o__max", "上游路由应恢复到 1M 变体");

  const noBeta = anthropicToUnified(
    { model: "openrouter-3o", messages: [{ role: "user", content: "hi" }] },
    "d",
  );
  assert.equal(noBeta.request.model, "openrouter-3o", "没有 context beta 时仍应钉住标准档");

  const alreadyExplicit = anthropicToUnified(
    { model: "openrouter-3o[1m]", messages: [{ role: "user", content: "hi" }] },
    "d",
    ["context-1m-2025-08-07"],
  );
  assert.equal(alreadyExplicit.request.model, "openrouter-3o[1m]", "显式 max 后缀不能重复追加");
});

test("Anthropic：Claude Code adaptive thinking 映射 output_config effort", () => {
  const parsed = anthropicToUnified({
    model: "openrouter-3o",
    messages: [{ role: "user", content: "hi" }],
    thinking: { type: "adaptive" },
    output_config: { effort: "max" },
  }, "d");

  assert.deepEqual(parsed.request.reasoning, { effort: "max" });
  assert.equal(parsed.request.reasoning_effort, "max");

  const legacy = anthropicToUnified({
    model: "openrouter-3o",
    messages: [{ role: "user", content: "hi" }],
    thinking: { type: "enabled", budget_tokens: 30_000 },
  }, "d");
  assert.deepEqual(legacy.request.reasoning, { effort: "high" });
  assert.equal(legacy.request.reasoning_effort, "high");
});

test("Anthropic：保留 system 和 message text block 的 cache_control", () => {
  const parsed = anthropicToUnified({
    model: "m",
    system: [
      { type: "text", text: "stable system", cache_control: { type: "ephemeral" } },
      { type: "text", text: "more system" },
    ],
    messages: [{
      role: "user",
      content: [{ type: "text", text: "hello", cache_control: { type: "ephemeral" } }],
    }],
  }, "d");

  const messages = parsed.request.messages ?? [];
  const system = messages[0]!.content as Array<Record<string, unknown>>;
  const user = messages[1]!.content as Array<Record<string, unknown>>;
  assert.equal(system.length, 2, "system block 不应被拼接后丢失断点位置");
  assert.deepEqual(system[0]!["cache_control"], { type: "ephemeral" });
  assert.equal(system[1]!["cache_control"], undefined);
  assert.deepEqual(user[0]!["cache_control"], { type: "ephemeral" });
});

test("三条路径的工具 schema 都要清洗（$ref / format 会让上游 400）", async () => {
  const dirty = { type: "object", $ref: "#/defs/X", format: "uri", $schema: "http://json-schema.org/", properties: {} };

  const a = anthropicToUnified(
    { model: "m", messages: [{ role: "user", content: "hi" }], tools: [{ name: "t", input_schema: dirty }] },
    "d",
  );
  const aParams = JSON.stringify(a.request.tools?.[0]?.function?.parameters);
  assert.ok(!aParams.includes("$ref") && !aParams.includes("format"), `Anthropic 未清洗: ${aParams}`);

  const r = responsesToUnified(
    { model: "m", input: "hi", tools: [{ type: "function", name: "t", parameters: dirty }] },
    "d",
  );
  const rParams = JSON.stringify(r.request.tools?.[0]?.function?.["parameters"]);
  assert.ok(!rParams.includes("$ref") && !rParams.includes("format"), `Responses 未清洗: ${rParams}`);
});

test("纯 $ref 节点与字符串 schema 也不能绕过清洗", () => {
  const a = anthropicToUnified({
    model: "m",
    messages: [{ role: "user", content: "hi" }],
    tools: [{ name: "t", input_schema: { type: "object", properties: { value: { $ref: "#/$defs/X" } } } }],
  }, "d");
  assert.deepEqual(
    (a.request.tools?.[0]?.function?.parameters as Record<string, unknown>)["properties"],
    { value: {} },
  );

  const chat = openaiChatToUnified({
    messages: [{ role: "user", content: "hi" }],
    tools: [{ type: "function", function: { name: "t", parameters: '{"$ref":"#/$defs/X"}' } }],
  }, "m");
  assert.deepEqual(chat.request.tools?.[0]?.function?.parameters, {});
});

test("Responses 多模态：音频键名、图片 detail 与工具结果媒体都正确", () => {
  const input = responsesToUnified({
    model: "m",
    input: [{
      type: "message",
      role: "user",
      content: [
        { type: "input_audio", input_audio: { data: "AAAA", format: "wav" } },
        { type: "input_image", image_url: "https://example.com/a.png", detail: "high" },
      ],
    }],
  }, "d");
  const parts = input.request.messages?.[0]?.content as Array<Record<string, unknown>>;
  assert.deepEqual(parts[0], { type: "input_audio", input_audio: { data: "AAAA", format: "wav" } });
  assert.deepEqual(parts[1], {
    type: "image_url",
    image_url: { url: "https://example.com/a.png", detail: "high" },
  });

  const toolOutput = responsesToUnified({
    model: "m",
    input: [
      { type: "function_call", call_id: "c1", name: "read", arguments: "{}" },
      {
        type: "function_call_output",
        call_id: "c1",
        output: [{ type: "input_image", image_url: "data:image/png;base64,AAAA", detail: "original" }],
      },
    ],
  }, "d");
  assert.deepEqual(toolOutput.request.messages?.map((message) => message.role), ["assistant", "tool", "user"]);
  assert.ok(JSON.stringify(toolOutput.request.messages?.[2]?.content).includes("data:image/png;base64,AAAA"));
});

test("Responses 无法解引用的 file 输入明确 400，而不是静默丢弃", () => {
  assert.throws(
    () => responsesToUnified({
      input: [{ type: "message", role: "user", content: [{ type: "input_file", file_id: "file_123" }] }],
    }, "m"),
    (error: Error) => error instanceof InvalidResponsesInputError &&
      (error as InvalidResponsesInputError).statusCode === 400,
  );
});

// ------------------------------------------------- Chat

test("Chat：同一份上游流的工具参数也不能丢", async () => {
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "exec", arguments: '{"cmd":' } }] }),
    output({ response: "text between" }),
    output({ tool_calls: [{ index: 0, function: { arguments: '"ls"}' } }] }),
    done("tool_calls"),
  );
  const res = await collectChatCompletion(streamOf(upstream), "m", 5000);
  assert.ok(res.ok);
  const choices = res.body["choices"] as Array<Record<string, unknown>>;
  const msg = choices[0]?.["message"] as Record<string, unknown>;
  const tc = (msg["tool_calls"] as Array<Record<string, unknown>>)[0];
  const fn = tc?.["function"] as Record<string, unknown>;
  assert.deepEqual(JSON.parse(fn["arguments"] as string), { cmd: "ls" });
});

test("Chat 流式以 [DONE] 结尾", async () => {
  const out = await drain(chatSseStream({
    upstreamBody: streamOf(sse(output({ response: "hi" }), done("stop"))),
    requestedModel: "m", includeUsage: false, idleMs: 5000,
  }));
  assert.ok(out.trimEnd().endsWith("data: [DONE]"));
});

test("Chat 所有正常上游终态都映射到标准 finish_reason", async () => {
  const cases = new Map([
    ["end_turn", "stop"],
    ["complete", "stop"],
    ["completed", "stop"],
    ["tool_use", "stop"],
    ["max_output_tokens", "length"],
    ["content_filtered", "content_filter"],
  ]);
  for (const [upstreamReason, expected] of cases) {
    const result = await collectChatCompletion(
      streamOf(sse(output({ response: "ok" }), done(upstreamReason))), "m", 5000,
    );
    assert.ok(result.ok);
    const choice = (result.body["choices"] as Array<Record<string, unknown>>)[0]!;
    assert.equal(choice["finish_reason"], expected, upstreamReason);
  }
});

test("Responses stream 的 added 初值为空，终态 output 顺序严格匹配 output_index", async () => {
  const upstream = sse(
    output({ tool_calls: [{ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: '{"x":' } }] }),
    output({ tool_calls: [{ index: 0, function: { arguments: "1}" } }] }),
    output({ reasoning_content: "late reasoning" }),
    output({ response: "late text" }),
    done("tool_calls"),
  );
  const frames = parseSse(await drain(responsesSseStream({
    upstreamBody: streamOf(upstream), requestedModel: "m", toolMeta: new Map(), idleMs: 5000,
  })));
  const added = frames.filter((frame) => frame.event === "response.output_item.added");
  const functionAdded = added.find((frame) =>
    (frame.data["item"] as Record<string, unknown>)["type"] === "function_call")!;
  assert.equal((functionAdded.data["item"] as Record<string, unknown>)["arguments"], "");

  const completed = frames.find((frame) => frame.event === "response.completed")!;
  const response = completed.data["response"] as Record<string, unknown>;
  const finalOutput = response["output"] as Array<Record<string, unknown>>;
  for (const frame of added) {
    const index = frame.data["output_index"] as number;
    const type = (frame.data["item"] as Record<string, unknown>)["type"];
    assert.equal(finalOutput[index]?.["type"], type, `output_index=${index}`);
  }
  assert.deepEqual(finalOutput.map((item) => item["type"]), ["reasoning", "message", "function_call"]);
});

// ---------------------------------------------------------------- stream 缺省值

/**
 * 三个协议的 `stream` 缺省值必须一致，而且都是 false。
 *
 * Responses 这条曾经被写成默认 true（还配了句「与 Chat Completions 相反」的注释），
 * 结果是：不传 stream 的标准 OpenAI SDK 拿到的是 SSE，JSON.parse 直接炸。
 * 因为 Codex 一直显式传 stream:true，这个偏差在真实流量里一次都没暴露过。
 */
test("三个协议的 stream 缺省都是 false", () => {
  const chat = openaiChatToUnified({ messages: [{ role: "user", content: "hi" }] }, "m");
  const anth = anthropicToUnified({ messages: [{ role: "user", content: "hi" }], max_tokens: 16 }, "m");
  const resp = responsesToUnified({ input: "hi" }, "m");

  assert.equal(chat.stream, false, "Chat Completions 缺省应为非流式");
  assert.equal(anth.stream, false, "Anthropic Messages 缺省应为非流式");
  assert.equal(resp.stream, false, "Responses 缺省应为非流式（官方规范如此）");
});

test("三个协议都认 stream:true", () => {
  assert.equal(openaiChatToUnified({ messages: [{ role: "user", content: "hi" }], stream: true }, "m").stream, true);
  assert.equal(anthropicToUnified({ messages: [{ role: "user", content: "hi" }], max_tokens: 16, stream: true }, "m").stream, true);
  assert.equal(responsesToUnified({ input: "hi", stream: true }, "m").stream, true);
});

test("stream:false 显式传入时三个协议都不走流式", () => {
  assert.equal(openaiChatToUnified({ messages: [{ role: "user", content: "hi" }], stream: false }, "m").stream, false);
  assert.equal(anthropicToUnified({ messages: [{ role: "user", content: "hi" }], max_tokens: 16, stream: false }, "m").stream, false);
  assert.equal(responsesToUnified({ input: "hi", stream: false }, "m").stream, false);
});

// ---------------------------------------------------------------- 非流式 Responses

// 这条路径在线上从未被执行过：默认 true 把所有请求都赶去了流式分支。
// 改回默认 false 之后它第一次会被真实客户端走到，所以这里补齐。

test("非流式 Responses 产出合规的 response 对象", async () => {
  const parsed = responsesToUnified({ input: "hi" }, "m");
  const r = await collectResponsesMessage(
    streamOf(sse(output({ content: "你好" }), output({ content: "世界" }), done("stop"))),
    "m",
    parsed.toolMeta,
    5000,
  );

  assert.equal(r.ok, true, "非流式收集不该失败");
  const body = (r as { body: Record<string, unknown> }).body;
  assert.equal(body.object, "response");
  assert.equal(body.status, "completed");
  assert.ok(typeof body.id === "string" && (body.id as string).startsWith("resp_"), "id 应是 resp_ 前缀");

  const out = body.output as Array<Record<string, unknown>>;
  const msg = out.find((o) => o.type === "message");
  assert.ok(msg, "应该有一个 message item");
  const text = (msg!.content as Array<Record<string, unknown>>)
    .filter((c) => c.type === "output_text")
    .map((c) => c.text as string)
    .join("");
  assert.equal(text, "你好世界", "文本增量应被拼接完整");
});

test("非流式 Responses 把工具调用放进 output，且不残留 _raw", async () => {
  const parsed = responsesToUnified(
    {
      input: "hi",
      tools: [{ type: "function", name: "get_weather", parameters: { type: "object", properties: { city: { type: "string" } } } }],
    },
    "m",
  );
  const r = await collectResponsesMessage(
    streamOf(sse(
      output({ tool_calls: [{ index: 0, id: "c1", type: "function", function: { name: "get_weather", arguments: '{"city":' } }] }),
      output({ tool_calls: [{ index: 0, function: { arguments: '"北京"}' } }] }),
      done("tool_calls"),
    )),
    "m",
    parsed.toolMeta,
    5000,
  );

  assert.equal(r.ok, true);
  const body = (r as { body: Record<string, unknown> }).body;
  const out = body.output as Array<Record<string, unknown>>;
  const call = out.find((o) => o.type === "function_call");
  assert.ok(call, "工具调用应该作为 function_call item 出现在 output 里");
  assert.equal(call!.name, "get_weather");
  assert.equal(call!.arguments, '{"city":"北京"}', "跨帧的参数增量应被拼完整");
  assert.ok(!JSON.stringify(body).includes("_raw"), "不该把内部字段 _raw 漏给客户端");
});
