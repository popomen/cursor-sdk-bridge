/**
 * buildUpstreamBody 的硬约束。每一条违反都会让上游静默失败或 400。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  adaptToolChoice,
  accessTypeFromEnv,
  asBool,
  buildUpstreamBody,
  contentPartsEmpty,
  lastUserText,
  normalizeContent,
  normalizeMessages,
  normalizeToolCalls,
  stringifyToolParams,
} from "../src/transform/upstream-body.js";

const OPTS = {
  config: "cfg",
  model: "mdl",
};

test("Q1: tools[].function.parameters 必须是 JSON 字符串", () => {
  const out = stringifyToolParams([
    { type: "function", function: { name: "f", parameters: { type: "object", properties: { a: { type: "string" } } } } },
  ]);
  const params = out[0]!.function!["parameters"];
  assert.equal(typeof params, "string");
  assert.deepEqual(JSON.parse(params as string), { type: "object", properties: { a: { type: "string" } } });
});

test("Q1: parameters 缺失时补默认对象后再字符串化", () => {
  const out = stringifyToolParams([{ type: "function", function: { name: "f" } }]);
  assert.equal(out[0]!.function!["parameters"], '{"type":"object","properties":{}}');
});

test("Q1: 已经是字符串的不重复编码", () => {
  const out = stringifyToolParams([{ type: "function", function: { name: "f", parameters: '{"type":"object"}' } }]);
  assert.equal(out[0]!.function!["parameters"], '{"type":"object"}');
});

test("Q1: 字符串 schema 也会清洗，function.strict 不下发上游", () => {
  const out = stringifyToolParams([{
    type: "function",
    function: { name: "f", strict: true, parameters: '{"type":"object","$ref":"#/X"}' },
  }]);
  assert.deepEqual(JSON.parse(out[0]!.function!["parameters"] as string), { type: "object" });
  assert.equal(out[0]!.function!["strict"], undefined);
});

test("Q2: tool_calls 的 function 键改名成 function_call", () => {
  const out = normalizeToolCalls([
    { id: "call_1", type: "function", index: 0, function: { name: "f", arguments: "{}" } },
  ]);
  assert.equal(out[0]!.function, undefined);
  assert.deepEqual(out[0]!.function_call, { name: "f", arguments: "{}" });
  // 其余键原样保留
  assert.equal(out[0]!.id, "call_1");
  assert.equal(out[0]!.index, 0);
});

test("Q3: 带 tool_calls 的 assistant，空 content 补成单个空格", () => {
  const msgs = normalizeMessages([
    { role: "assistant", content: "", tool_calls: [{ id: "c1", function: { name: "f", arguments: "{}" } }] },
  ]);
  assert.deepEqual(msgs[0]!.content, [{ type: "text", text: " " }]);
});

test("Q3: 没有 tool_calls 的 assistant 空 content 不补空格", () => {
  const msgs = normalizeMessages([{ role: "assistant", content: "" }]);
  assert.deepEqual(msgs[0]!.content, [{ type: "text", text: "" }]);
});

test("Q8: 空 output 的 tool 消息补成单个空格", () => {
  const msgs = normalizeMessages([{ role: "tool", content: "", tool_call_id: "c1" }]);
  assert.deepEqual(msgs[0]!.content, [{ type: "text", text: " " }]);
});

test("Q8: 非空 output 的 tool 消息不动", () => {
  const msgs = normalizeMessages([{ role: "tool", content: "ok", tool_call_id: "c1" }]);
  assert.deepEqual(msgs[0]!.content, [{ type: "text", text: "ok" }]);
});

test("Q4: 普通 tool_choice 归一成 raw-chat 字符串", () => {
  assert.equal(adaptToolChoice("auto"), "auto");
  assert.equal(adaptToolChoice("none"), "none");
  assert.equal(adaptToolChoice("required"), "required");
  assert.equal(adaptToolChoice({ type: "function", function: { name: "get_weather" } }), "get_weather");
  assert.equal(adaptToolChoice({ name: "fallback" }), "fallback");

  const body = buildUpstreamBody({
    messages: [],
    tools: [{ type: "function", function: { name: "lookup" } }],
    tool_choice: "none",
  }, OPTS);
  assert.equal(body["tool_choice"], "none");
  assert.equal((body["tools"] as unknown[]).length, 1, "none 仍要保留工具定义");
  assert.equal((body["messages"] as unknown[]).length, 0, "不再插入 system 指令");
});

test("Q4: structured output 合成函数工具并覆盖 tool_choice", () => {
  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] };
  const chat = buildUpstreamBody({
    messages: [],
    response_format: { type: "json_schema", json_schema: { name: "answer", schema } },
  }, OPTS);
  const chatTools = chat["tools"] as Array<{ function: { name: string; parameters: string } }>;
  assert.equal(chat["tool_choice"], "structured_output");
  assert.equal(chat["parallel_tool_calls"], undefined);
  assert.equal(chatTools.length, 1);
  assert.equal(chatTools[0]!.function.name, "structured_output");
  assert.deepEqual(JSON.parse(chatTools[0]!.function.parameters), schema);

  const jsonObject = buildUpstreamBody({
    messages: [],
    response_format: { type: "json_object" },
  }, OPTS);
  const jsonObjectTools = jsonObject["tools"] as Array<{ function: { parameters: string } }>;
  assert.deepEqual(JSON.parse(jsonObjectTools[0]!.function.parameters), { type: "object" });

  const responses = buildUpstreamBody({
    messages: [],
    tools: [{ type: "function", function: { name: "lookup" } }],
    tool_choice: "none",
    text: { format: { type: "json_schema", name: "answer", schema } },
  }, OPTS);
  const responseTools = responses["tools"] as Array<{ function: { name: string } }>;
  assert.equal(responses["tool_choice"], "required");
  assert.equal(responses["parallel_tool_calls"], true);
  assert.deepEqual(responseTools.map((tool) => tool.function.name), ["lookup", "structured_output"]);
});

test("Q5: user_input 取最后一条 user 消息，多个 text part 用换行连接", () => {
  const msgs = normalizeMessages([
    { role: "user", content: "first" },
    { role: "assistant", content: "reply" },
    { role: "user", content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] },
  ]);
  assert.equal(lastUserText(msgs), "a\nb");
});

test("Q5: 图片 part 不贡献 user_input", () => {
  const msgs = normalizeMessages([
    { role: "user", content: [{ type: "image_url", image_url: { url: "data:image/png;base64,AA" } }] },
  ]);
  assert.equal(lastUserText(msgs), "");
});

test("Q6: 显式 max_tokens 原样下发", () => {
  const body = buildUpstreamBody({ messages: [], max_tokens: 10 }, OPTS);
  assert.equal(body["max_tokens"], 10);
});

test("Q6: max_tokens 缺失时默认 32768", () => {
  const body = buildUpstreamBody({ messages: [] }, OPTS);
  assert.equal(body["max_tokens"], 32768);
});

test("Q7: content 永远归一化成数组", () => {
  assert.deepEqual(normalizeContent("s"), [{ type: "text", text: "s" }]);
  assert.deepEqual(normalizeContent(null), [{ type: "text", text: "" }]);
  assert.deepEqual(normalizeContent(42), [{ type: "text", text: "42" }]);
});

test("白名单外的 part type 被 JSON 序列化成 text", () => {
  const out = normalizeContent([{ type: "input_text", text: "x" }]);
  assert.equal((out[0] as Record<string, unknown>)["type"], "text");
  assert.equal((out[0] as Record<string, unknown>)["text"], '{"type":"input_text","text":"x"}');
});

test("白名单内的 part 原样保留（含 detail 等字段）", () => {
  const part = { type: "image_url", image_url: { url: "http://x/y.png", detail: "high" } };
  assert.deepEqual(normalizeContent([part])[0], part);
});

test("纯图片消息不算空", () => {
  assert.equal(contentPartsEmpty([{ type: "image_url", image_url: { url: "x" } }]), false);
  assert.equal(contentPartsEmpty([{ type: "text", text: "" }]), true);
  assert.equal(contentPartsEmpty([]), true);
});

test("有 tools 时自动补 parallel_tool_calls", () => {
  const body = buildUpstreamBody(
    { messages: [], tools: [{ type: "function", function: { name: "f" } }] },
    OPTS,
  );
  assert.equal(body["parallel_tool_calls"], true);
});

test("上游必填字段齐全", () => {
  const body = buildUpstreamBody({ messages: [{ role: "user", content: "hi" }] }, OPTS);
  for (const k of ["config_name", "model_name", "session_id", "conversation_id", "messages", "user_input", "max_tokens", "is_preset", "access_type"]) {
    assert.ok(k in body, `missing ${k}`);
  }
  assert.equal(body["is_preset"], true);
});

test("A1: access_type 仅接受 u8 环境覆盖，非法值回退 4", () => {
  assert.equal(accessTypeFromEnv({}), 4);
  assert.equal(accessTypeFromEnv({ TRAEX_BRIDGE_ACCESS_TYPE: "9" }), 9);
  for (const raw of ["-1", "256", "1.5", "invalid"]) {
    assert.equal(accessTypeFromEnv({ TRAEX_BRIDGE_ACCESS_TYPE: raw }), 4, raw);
  }
});

test("A2: raw-chat 只透传扁平 reasoning_effort 并归一 ultra", () => {
  const body = buildUpstreamBody({
    messages: [],
    temperature: 0.1,
    top_p: 0.2,
    seed: 7,
    reasoning: { effort: "ultra" },
    reasoning_effort: "ultra",
    text: { verbosity: "high" },
    store: true,
    include: ["reasoning.encrypted_content"],
    client_metadata: { client: "test" },
  }, OPTS);
  assert.equal(body["reasoning_effort"], "max");
  for (const key of ["temperature", "top_p", "seed", "reasoning", "text", "store", "include", "client_metadata"]) {
    assert.equal(body[key], undefined, key);
  }
});

test("每次调用都生成互不复用的 Session 与 conversation", () => {
  const first = buildUpstreamBody({ messages: [{ role: "user", content: "hi" }] }, OPTS);
  const second = buildUpstreamBody({ messages: [{ role: "user", content: "hi" }] }, OPTS);
  for (const value of [first["session_id"], first["conversation_id"], second["session_id"], second["conversation_id"]]) {
    assert.match(String(value), /^[0-9a-f-]{36}$/i);
  }
  assert.notEqual(first["session_id"], first["conversation_id"]);
  assert.notEqual(first["session_id"], second["session_id"]);
  assert.notEqual(first["conversation_id"], second["conversation_id"]);
});

test("stateless conversation 不自动创建缓存断点", () => {
  const body = buildUpstreamBody(
    { messages: [{ role: "user", content: "one-shot" }] },
    OPTS,
  );
  const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  assert.equal(messages[0]!.content[0]!["cache_control"], undefined);
});

test("stateless 模式仍保留客户端显式 cache_control", () => {
  const body = buildUpstreamBody(
    {
      messages: [{
        role: "user",
        content: [{ type: "text", text: "explicit", cache_control: { type: "ephemeral" } }],
      }],
    },
    OPTS,
  );
  const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  assert.deepEqual(messages[0]!.content[0]!["cache_control"], { type: "ephemeral" });
});

test("stateless 模式不会自动给最后一个 text part 添加缓存断点", () => {
  const body = buildUpstreamBody(
    {
      messages: [
        { role: "system", content: "stable prefix" },
        { role: "user", content: "turn one" },
      ],
    },
    OPTS,
  );
  const messages = body["messages"] as Array<Record<string, unknown>>;
  assert.equal(messages.some((message) => "cache_control" in message), false);
  const first = messages[0]!["content"] as Array<Record<string, unknown>>;
  const last = messages[1]!["content"] as Array<Record<string, unknown>>;
  assert.equal(first[0]!["cache_control"], undefined);
  assert.equal(last[0]!["cache_control"], undefined);
});

test("只保留客户端已有缓存断点，不补齐末尾 text part", () => {
  const body = buildUpstreamBody(
    {
      messages: [
        {
          role: "system",
          content: [{ type: "text", text: "stable system", cache_control: { type: "ephemeral" } }],
        },
        { role: "user", content: [{ type: "text", text: "latest turn" }] },
      ],
    },
    OPTS,
  );
  const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  assert.deepEqual(messages[0]!.content[0]!["cache_control"], { type: "ephemeral" });
  assert.equal(messages[1]!.content[0]!["cache_control"], undefined);
});

test("客户端已有四个缓存断点时保持原样", () => {
  const marked = (text: string) => ({ type: "text", text, cache_control: { type: "ephemeral" } });
  const body = buildUpstreamBody(
    {
      messages: [
        { role: "system", content: [marked("s1"), marked("s2"), marked("s3"), marked("s4")] },
        { role: "user", content: [{ type: "text", text: "latest turn" }] },
      ],
    },
    OPTS,
  );
  const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  assert.equal(messages[1]!.content[0]!["cache_control"], undefined);
});

test("不向 user 或 tool 消息自动写缓存断点", () => {
  const body = buildUpstreamBody(
    {
      messages: [
        { role: "user", content: [{ type: "text", text: "call tool" }] },
        { role: "tool", tool_call_id: "call-1", content: [{ type: "text", text: "tool output" }] },
      ],
    },
    OPTS,
  );
  const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  assert.equal(messages[0]!.content[0]!["cache_control"], undefined);
  assert.equal(messages[1]!.content[0]!["cache_control"], undefined);
});

test("prompt_cache_key 不下发 Trae raw chat", () => {
  const body = buildUpstreamBody(
    { messages: [{ role: "user", content: "hi" }], prompt_cache_key: "client-cache-key" },
    OPTS,
  );
  assert.equal(body["prompt_cache_key"], undefined);
});

test("asBool 只认 true/'true'/'1'", () => {
  assert.equal(asBool(true), true);
  assert.equal(asBool("true"), true);
  assert.equal(asBool("1"), true);
  assert.equal(asBool(1), false); // 数字 1 不算真
  assert.equal(asBool("yes"), false);
  assert.equal(asBool(undefined), false);
});

test("developer role 归一成 system（OpenAI o1+ 用它取代 system）", () => {
  // Codex 发的 Responses input 第一条就是 developer；上游只认
  // system/assistant/user/tool，有的模型直接 400，有的容忍，必须在这里收敛
  const msgs = normalizeMessages([
    { role: "developer", content: "You are a helpful assistant." },
    { role: "user", content: "hi" },
  ]);
  assert.equal(msgs[0]!.role, "system");
  assert.equal(msgs[1]!.role, "user");
});

test("developer 归一后 tool_choice 不再插入额外 system 指令", () => {
  const body = buildUpstreamBody(
    {
      messages: [{ role: "developer", content: "rules" }, { role: "user", content: "hi" }],
      tools: [{ type: "function", function: { name: "f" } }],
      tool_choice: "required",
    },
    OPTS,
  );
  const msgs = body["messages"] as Array<{ role: string }>;
  assert.deepEqual(msgs.map((message) => message.role), ["system", "user"]);
  assert.equal(body["tool_choice"], "required");
});

test("连续 assistant 合并成单轮，正文和并行工具调用都不丢", () => {
  const msgs = normalizeMessages([
    { role: "user", content: "go" },
    { role: "assistant", content: "先说明" },
    {
      role: "assistant",
      content: "",
      tool_calls: [{ id: "a", function: { name: "f1", arguments: "{}" } }],
    },
    {
      role: "assistant",
      content: "再说明",
      tool_calls: [{ id: "b", function: { name: "f2", arguments: "{}" } }],
    },
  ]);

  assert.deepEqual(msgs.map((message) => message.role), ["user", "assistant"]);
  const assistant = msgs[1]!;
  const text = (assistant.content as Array<Record<string, unknown>>)
    .map((part) => part["text"])
    .join("");
  assert.equal(text, "先说明再说明");
  assert.deepEqual(assistant.tool_calls?.map((tool) => tool.index), [0, 1]);
  assert.deepEqual(assistant.tool_calls?.map((tool) => tool.id), ["a", "b"]);
});

test("Chat tool 消息里的媒体抽成连续 tool 段后的 user 消息", () => {
  const msgs = normalizeMessages([
    { role: "assistant", content: "", tool_calls: [
      { id: "a", function: { name: "read", arguments: "{}" } },
      { id: "b", function: { name: "read", arguments: "{}" } },
    ] },
    { role: "tool", tool_call_id: "a", content: [{ type: "image_url", image_url: { url: "data:image/png;base64,A" } }] },
    { role: "tool", tool_call_id: "b", content: [{ type: "image_url", image_url: { url: "data:image/png;base64,B" } }] },
  ]);
  assert.deepEqual(msgs.map((message) => message.role), ["assistant", "tool", "tool", "user"]);
  assert.ok(JSON.stringify(msgs[3]!.content).includes("data:image/png;base64,A"));
  assert.ok(JSON.stringify(msgs[3]!.content).includes("data:image/png;base64,B"));
});
