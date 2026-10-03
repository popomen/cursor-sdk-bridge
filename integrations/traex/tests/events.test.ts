/**
 * 上游事件归一化。tool call delta 的四种 wire 形状是最容易静默出错的地方。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  ToolCallAccumulator,
  classifyUpstreamError,
  classifyUpstreamErrorEvent,
  formatUpstreamErrorEvent,
  isQueueWaitEventName,
  parseOutputEvent,
  parseQueueEvent,
  parseToolCallDelta,
  parseUsage,
  queuePositionFromMessage,
} from "../src/upstream/events.js";

test("output 事件：新旧字段双兼容，旧字段优先", () => {
  const both = parseOutputEvent({ response: "old", content: "new", reasoning_content: "oldR", reasoning: "newR" });
  assert.equal(both.response, "old");
  assert.equal(both.reasoningContent, "oldR");

  const onlyNew = parseOutputEvent({ content: "new", reasoning: "newR" });
  assert.equal(onlyNew.response, "new");
  assert.equal(onlyNew.reasoningContent, "newR");
});

test("tool delta 形状 A: function_call 对象", () => {
  const d = parseToolCallDelta({
    index: 1, id: "call_1", type: "function",
    function_call: { name: "send", arguments: '{"a":' },
  })!;
  assert.equal(d.index, 1);
  assert.equal(d.id, "call_1");
  assert.equal(d.name, "send");
  assert.equal(d.arguments, '{"a":');
});

test("tool delta 形状 B: 只有 function.arguments 的续帧", () => {
  const d = parseToolCallDelta({ index: 1, function: { arguments: '"x"}' } })!;
  assert.equal(d.arguments, '"x"}');
  assert.equal(d.name, "");
});

test("tool delta 形状 C: 顶层扁平 tool_call_id + name + arguments", () => {
  const d = parseToolCallDelta({ index: 2, tool_call_id: "call_2", name: "exec", arguments: '{"cmd":"pwd"}' })!;
  assert.equal(d.id, "call_2");
  assert.equal(d.name, "exec");
  assert.equal(d.arguments, '{"cmd":"pwd"}');
});

test("tool delta 形状 D: input 作为 arguments 的别名", () => {
  const d = parseToolCallDelta({ index: 3, name: "apply_patch", input: "*** Begin Patch" })!;
  assert.equal(d.arguments, "*** Begin Patch");
});

test("function 的优先级高于 function_call，空值不覆盖", () => {
  const d = parseToolCallDelta({
    index: 0,
    function_call: { name: "fromFc", arguments: "A" },
    function: { name: "", arguments: "B" },
  })!;
  assert.equal(d.name, "fromFc"); // function.name 是空串，不覆盖
  assert.equal(d.arguments, "B");  // function.arguments 非空，覆盖
});

test("首帧 function_call + 续帧 function 必须拼成完整 JSON", () => {
  // 这是最容易翻车的场景：只认 function_call 会丢掉后续所有片段
  const acc = new ToolCallAccumulator();
  acc.apply(parseToolCallDelta({ index: 0, id: "c1", type: "function", function_call: { name: "f", arguments: '{"target":' } })!);
  acc.apply(parseToolCallDelta({ index: 0, function: { arguments: '"/root",' } })!);
  acc.apply(parseToolCallDelta({ index: 0, function: { arguments: '"message":"ok"}' } })!);

  const got = acc.get(0)!;
  assert.equal(got.id, "c1");
  assert.equal(got.name, "f");
  assert.deepEqual(JSON.parse(got.arguments), { target: "/root", message: "ok" });
});

test("累积器按 index 分桶且升序输出", () => {
  const acc = new ToolCallAccumulator();
  acc.apply({ index: 2, id: "b", type: "function", name: "second", arguments: "{}" });
  acc.apply({ index: 0, id: "a", type: "function", name: "first", arguments: "{}" });
  const sorted = acc.sorted();
  assert.deepEqual(sorted.map((t) => t.index), [0, 2]);
  assert.equal(sorted[0]!.name, "first");
});

test("缺失的 tool id 只生成一次，迟到的上游 id 不会让流式调用改名", () => {
  let generated = 0;
  const acc = new ToolCallAccumulator(() => `call_synthetic_${++generated}`);

  const first = acc.apply({
    index: 0,
    id: "",
    type: "function",
    name: "lookup",
    arguments: '{"id":',
  });
  const continuation = acc.apply({
    index: 0,
    id: "late-upstream-id",
    type: "",
    name: "",
    arguments: "1}",
  });

  assert.deepEqual(first, { index: 0, id: "call_synthetic_1", isNew: true });
  assert.deepEqual(continuation, { index: 0, id: "call_synthetic_1", isNew: false });
  assert.equal(acc.get(0)!.id, "call_synthetic_1");
  assert.deepEqual(JSON.parse(acc.get(0)!.arguments), { id: 1 });

  const second = acc.apply({ index: 1, id: "", type: "function", name: "other", arguments: "{}" });
  const upstream = acc.apply({ index: 2, id: "call_upstream", type: "function", name: "raw", arguments: "{}" });
  assert.equal(second.id, "call_synthetic_2");
  assert.equal(upstream.id, "call_upstream");
  assert.equal(generated, 2, "已有上游 id 时不应调用 synthetic id factory");
});

test("usage: total 为 0 时由 prompt+completion 推导", () => {
  const u = parseUsage({ prompt_tokens: 10, completion_tokens: 5 });
  assert.equal(u.total_tokens, 15);
});

test("usage: cache_write_tokens 是 cache_write_input_tokens 的别名", () => {
  assert.equal(parseUsage({ cache_write_tokens: 7 }).cache_write_input_tokens, 7);
  assert.equal(parseUsage({ cache_write_input_tokens: 3, cache_write_tokens: 7 }).cache_write_input_tokens, 3);
});

test("usage: cache_creation_input_tokens 是 TraeX/Anthropic 的缓存写入别名", () => {
  assert.equal(parseUsage({ cache_creation_input_tokens: 11 }).cache_write_input_tokens, 11);
  assert.equal(
    parseUsage({ cache_write_input_tokens: 3, cache_creation_input_tokens: 11 }).cache_write_input_tokens,
    3,
  );
});

test("usage: cached_input_tokens 是 TraeX 的缓存读取别名", () => {
  assert.equal(parseUsage({ cached_input_tokens: 13 }).cache_read_input_tokens, 13);
  assert.equal(
    parseUsage({ cache_read_input_tokens: 5, cached_input_tokens: 13 }).cache_read_input_tokens,
    5,
  );
});

test("排队事件名模糊匹配，但 queue_end 不算", () => {
  assert.equal(isQueueWaitEventName("queue_begin"), true);
  assert.equal(isQueueWaitEventName("request_wait_in_queue"), true);
  assert.equal(isQueueWaitEventName("some_queue_wait_thing"), true);
  assert.equal(isQueueWaitEventName("queue_end"), false);
  assert.equal(isQueueWaitEventName("output"), false);
});

test("position 缺失时从 message 文本里抠", () => {
  assert.equal(
    queuePositionFromMessage("Too many requests right now. You're in the queue at position 908."),
    908,
  );
  // 真实上游文案
  assert.equal(
    queuePositionFromMessage("Too many current requests. Your queue position is 981. Please wait for a while."),
    981,
  );
});

test("position 后面跟非数字时放弃，避免误吃 retry delay", () => {
  assert.equal(queuePositionFromMessage("position unknown, retry in 30s"), undefined);
});

test("queue 事件 position 字段名三种拼写都认", () => {
  assert.equal(parseQueueEvent("q", { position: 5 }).position, 5);
  assert.equal(parseQueueEvent("q", { queue_position: 6 }).position, 6);
  assert.equal(parseQueueEvent("q", { queuePosition: 7 }).position, 7);
});

test("错误分类顺序：quota 优先于 rate_limit", () => {
  // 额度耗尽可能带 429 状态，必须标成 quota 而不是泛化的 rate_limit
  assert.equal(classifyUpstreamError(429, "usage limit used up"), "quota");
});

test("错误分类顺序：invalid_request 优先于 rate_limit", () => {
  // Trae 的聚合错误体会给每次尝试都追加 rate limited，真实原因却是我们的 400
  const payload =
    '{"error":"all models failed","model_call_chain":[{"error":"400 Bad Request {missing_required_parameter}\\nmodel QPM/TPM rate limited"}]}';
  assert.equal(classifyUpstreamError(200, payload), "invalid_request");
});

test("错误分类：auth 与无匹配", () => {
  assert.equal(classifyUpstreamError(401, "unauthorized"), "auth");
  assert.equal(classifyUpstreamError(500, "internal server error"), "");
});

test("SSE 数值业务码优先于文本启发式", () => {
  assert.equal(classifyUpstreamError(200, '{"code":4008,"message":"rate limited"}'), "quota");
  assert.equal(classifyUpstreamError(200, '{"code":4037,"message":"usage limit used up"}'), "invalid_request");
  assert.equal(classifyUpstreamError(200, '{"code":4050,"message":"the request queue size is exceed"}'), "rate_limit");

  const quota = classifyUpstreamErrorEvent('{"code":4008,"message":"rate limited"}');
  assert.equal(quota.terminal, true);
  assert.equal(quota.code, 4008);

  const model = classifyUpstreamErrorEvent('{"code":4037,"message":"x"}');
  assert.equal(model.category, "invalid_request");
  assert.equal(model.terminal, true);

  const queue = classifyUpstreamErrorEvent('{"code":4050,"message":"the request queue size is exceed"}');
  assert.equal(queue.category, "rate_limit");
  assert.equal(queue.terminal, true);
});

test("SSE error 超窗文案升为 invalid_request 且立即终止，无论数值码", () => {
  for (const payload of [
    '{"code":5001,"message":"context_length_exceeded"}',
    '{"code":4008,"message":"prompt is too long"}',
    '{"message":"context window exceeded"}',
  ]) {
    const classified = classifyUpstreamErrorEvent(payload);
    assert.equal(classified.category, "invalid_request", payload);
    assert.equal(classified.terminal, true, payload);
    assert.equal(classifyUpstreamError(200, payload), "invalid_request", payload);
  }
});

test("SSE error 空 message 给出可读兜底并保留 code", () => {
  const quota = classifyUpstreamErrorEvent('{"code":4008,"message":""}');
  assert.equal(quota.category, "quota");
  assert.match(quota.message, /额度/);
  assert.equal(formatUpstreamErrorEvent('{"code":4008,"message":""}'), `${quota.message} (code=4008)`);

  const model = classifyUpstreamErrorEvent('{"code":4037}');
  assert.equal(model.category, "invalid_request");
  assert.match(model.message, /仓库|模型/);
  assert.match(formatUpstreamErrorEvent('{"code":4037}'), /code=4037/);

  const queue = classifyUpstreamErrorEvent('{"code":4050,"message":""}');
  assert.match(queue.message, /queue/i);
  assert.match(formatUpstreamErrorEvent('{"code":4050,"message":""}'), /code=4050/);
});

test("SSE 4001 参数非法映射 invalid_request 且立即终止", () => {
  const classified = classifyUpstreamErrorEvent('{"code":4001,"message":"param is invalid"}');
  assert.equal(classified.category, "invalid_request");
  assert.equal(classified.terminal, true);
  assert.equal(classified.code, 4001);
  assert.equal(classifyUpstreamError(200, '{"code":4001,"message":"param is invalid"}'), "invalid_request");
  // 数值码优先于文本启发式：4001 即使带 quota 文案也不是额度耗尽
  assert.equal(classifyUpstreamError(200, '{"code":4001,"message":"usage limit used up"}'), "invalid_request");

  const empty = classifyUpstreamErrorEvent('{"code":4001,"message":""}');
  assert.equal(empty.category, "invalid_request");
  assert.equal(empty.terminal, true);
  assert.match(empty.message, /invalid/i);
  assert.match(formatUpstreamErrorEvent('{"code":4001,"message":""}'), /code=4001/);
});

test("未知数值码不终止，交给文本启发式", () => {
  const unknown = classifyUpstreamErrorEvent('{"code":5001,"message":"internal boom"}');
  assert.equal(unknown.terminal, false);
  assert.equal(unknown.category, "");
  assert.equal(unknown.code, 5001);
  assert.equal(classifyUpstreamError(500, '{"code":5001,"message":"internal boom"}'), "");
});
