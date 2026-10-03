/**
 * App 边界回归：验证真实 HTTP header 经过协议转换、模型解析后，最终落到正确上游。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { buildApp } from "../src/app.js";
import type { Config } from "../src/config.js";
import { CatalogStore } from "../src/upstream/catalog.js";
import type { UpstreamClient } from "../src/upstream/client.js";
import { QuotaCache } from "../src/upstream/quota.js";
import { UsageStore } from "../src/upstream/usage.js";

const config: Config = {
  host: "127.0.0.1",
  port: 8317,
  apiKey: "test-key",
  defaultModel: "openrouter-3o",
  upstreamBaseUrl: "https://example.invalid",
  catalogBaseUrl: "https://example.invalid",
  traePpeEnv: "",
  credentialSource: "environment",
  traeToken: "test-token",
  traeTokenFile: "",
  traeTokenCommand: "",
  bytedcliBin: "",
  tokenCommandTimeoutMs: 1_000,
  stateDir: "/tmp/traex-bridge-app-routing-test",
  maxRequestBytes: 1_000_000,
  upstreamHeaderTimeoutMs: 5_000,
  streamIdleTimeoutMs: 5_000,
  responsesDeferLifecycle: false,
  logLevel: "silent",
  logRequests: false,
  dumpBody: false,
  trace: false,
};

test("Anthropic context-1m + adaptive effort 路由 max 变体且只发扁平 effort", async (t) => {
  const catalog = new CatalogStore();
  catalog.set([
    {
      id: "openrouter-3o",
      config: "openrouter-3o",
      model: "openrouter-3o__dev",
      contextWindow: 168000,
      usageLimited: false,
      repoRestricted: false,
      superModel: false,
      feeLevel: 0,
      manualUsage: 0,
    },
    {
      id: "openrouter-3o[1m]",
      config: "openrouter-3o",
      model: "openrouter-3o__max",
      contextWindow: 936000,
      usageLimited: false,
      repoRestricted: false,
      superModel: false,
      feeLevel: 0,
      manualUsage: 0,
    },
  ]);

  let upstreamBody: Record<string, unknown> | undefined;
  const fakeClient = {
    async chat(body: unknown): Promise<Response> {
      upstreamBody = body as Record<string, unknown>;
      const sse = [
        'event: output\ndata: {"response":"ok"}\n\n',
        'event: token_usage\ndata: {"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}\n\n',
        'event: done\ndata: {"finish_reason":"stop"}\n\n',
      ].join("");
      return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
    },
    async getAccountUsage(): Promise<never> {
      throw new Error("quota must not be requested in this test");
    },
  } as unknown as UpstreamClient;

  const usage = new UsageStore();
  const app = buildApp({
    config,
    client: fakeClient,
    catalog,
    quota: new QuotaCache(fakeClient, catalog, usage),
    usage,
  });
  t.after(async () => app.close());

  const health = await app.inject({ method: "GET", url: "/healthz" });
  assert.equal(health.statusCode, 200);
  assert.equal(health.json()["generation_mode"], "stateless");
  assert.equal(health.json()["affinity_mode"], undefined);

  const queue = await app.inject({
    method: "GET",
    url: "/v1/queue",
    headers: { authorization: `Bearer ${config.apiKey}` },
  });
  assert.equal(queue.statusCode, 410);
  assert.equal(queue.json()["stateless"], true);

  const response = await app.inject({
    method: "POST",
    url: "/v1/messages",
    headers: {
      "x-api-key": config.apiKey,
      "anthropic-version": "2023-06-01",
      "anthropic-beta": "context-1m-2025-08-07",
    },
    payload: {
      model: "openrouter-3o",
      max_tokens: 256,
      messages: [{ role: "user", content: "reply ok" }],
      thinking: { type: "adaptive", display: "omitted" },
      output_config: { effort: "max" },
    },
  });

  assert.equal(response.statusCode, 200);
  assert.equal(upstreamBody?.["config_name"], "openrouter-3o");
  assert.equal(upstreamBody?.["model_name"], "openrouter-3o__max");
  assert.equal(upstreamBody?.["reasoning"], undefined);
  assert.equal(upstreamBody?.["reasoning_effort"], "max");

  const responseBody = response.json() as Record<string, unknown>;
  assert.equal(responseBody["model"], "openrouter-3o", "响应必须回显客户端请求的 base 名");
  const observed = usage.snapshot()[0]!;
  assert.equal(observed.requests, 1);
  assert.equal(observed.total_tokens, 12);
});

test("所有协议每轮都使用全新的 Session 和 conversation", async (t) => {
  const catalog = new CatalogStore();
  catalog.set([{
    id: "openrouter-3o",
    config: "openrouter-3o",
    model: "openrouter-3o__dev",
    contextWindow: 168000,
    usageLimited: false,
    repoRestricted: false,
    superModel: false,
    feeLevel: 0,
    manualUsage: 0,
  }]);

  const upstreamBodies: Array<Record<string, unknown>> = [];
  const fakeClient = {
    async chat(body: unknown): Promise<Response> {
      const upstream = body as Record<string, unknown>;
      upstreamBodies.push(upstream);
      const messages = upstream["messages"] as Array<Record<string, unknown>>;
      const hasToolResult = messages.some((message) => message["role"] === "tool");
      const requestsTool = upstream["user_input"] === "ask tool" && !hasToolResult;
      const sse = requestsTool
        ? [
            'event: output\ndata: {"tool_calls":[{"index":0,"type":"function","function_call":{"name":"lookup","arguments":"{}"}}]}\n\n',
            'event: done\ndata: {"finish_reason":"tool_calls"}\n\n',
          ].join("")
        : [
            'event: output\ndata: {"response":"ok"}\n\n',
            'event: done\ndata: {"finish_reason":"stop"}\n\n',
          ].join("");
      return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
    },
    async getAccountUsage(): Promise<never> {
      throw new Error("quota must not be requested in this test");
    },
  } as unknown as UpstreamClient;

  const usage = new UsageStore();
  const app = buildApp({
    config,
    client: fakeClient,
    catalog,
    quota: new QuotaCache(fakeClient, catalog, usage),
    usage,
    credentials: { expiresInDays: () => null },
  });
  t.after(async () => app.close());

  const claudeRequest = async (sessionId: string) => app.inject({
    method: "POST",
    url: "/v1/messages",
    headers: { "x-api-key": config.apiKey, "anthropic-version": "2023-06-01" },
    payload: {
      model: "openrouter-3o",
      max_tokens: 256,
      metadata: { user_id: JSON.stringify({ session_id: sessionId }) },
      messages: [{ role: "user", content: "reply ok" }],
    },
  });

  assert.equal((await claudeRequest("claude-A")).statusCode, 200);
  assert.equal((await claudeRequest("claude-A")).statusCode, 200);
  assert.equal((await claudeRequest("claude-B")).statusCode, 200);

  const claudeSessions = upstreamBodies.map((body) => body["session_id"]);
  assert.notEqual(claudeSessions[0], claudeSessions[1]);
  assert.notEqual(claudeSessions[0], claudeSessions[2]);
  const claudeConversations = upstreamBodies.map((body) => body["conversation_id"]);
  assert.notEqual(claudeConversations[0], claudeConversations[1]);
  assert.notEqual(claudeConversations[0], claudeConversations[2]);
  assert.deepEqual(upstreamBodies.map((body) => body["prompt_cache_key"]), [undefined, undefined, undefined]);

  const codexRequest = async (sessionId: string) => app.inject({
    method: "POST",
    url: "/v1/responses",
    headers: { authorization: `Bearer ${config.apiKey}` },
    payload: {
      model: "openrouter-3o",
      input: "reply ok",
      client_metadata: { session_id: sessionId },
      prompt_cache_key: "client-cache-key",
    },
  });

  assert.equal((await codexRequest("codex-A")).statusCode, 200);
  assert.equal((await codexRequest("codex-A")).statusCode, 200);
  const codexSessions = upstreamBodies.slice(3).map((body) => body["session_id"]);
  assert.notEqual(codexSessions[0], codexSessions[1]);
  assert.notEqual(codexSessions[0], claudeSessions[0]);
  const codexConversations = upstreamBodies.slice(3).map((body) => body["conversation_id"]);
  assert.notEqual(codexConversations[0], codexConversations[1]);
  assert.notEqual(codexConversations[0], claudeConversations[0]);
  assert.deepEqual(upstreamBodies.slice(3).map((body) => body["prompt_cache_key"]), [undefined, undefined]);

  const sharedCacheRequest = (input: string) => app.inject({
    method: "POST",
    url: "/v1/responses",
    headers: { authorization: `Bearer ${config.apiKey}` },
    payload: { model: "openrouter-3o", input, prompt_cache_key: "shared-prefix-bucket" },
  });
  assert.equal((await sharedCacheRequest("independent one")).statusCode, 200);
  assert.equal((await sharedCacheRequest("independent two")).statusCode, 200);
  const sharedCacheBodies = upstreamBodies.slice(5, 7);
  assert.notEqual(
    sharedCacheBodies[0]!["conversation_id"],
    sharedCacheBodies[1]!["conversation_id"],
    "共享 prompt_cache_key 不能把独立请求串成一个逻辑会话",
  );
  for (const body of sharedCacheBodies) {
    const messages = body["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
    assert.equal(messages[0]!.content[0]!["cache_control"], undefined, "匿名 conversation 不应自动写 cache");
  }

  const toolRequest = await app.inject({
    method: "POST",
    url: "/v1/chat/completions",
    headers: { authorization: `Bearer ${config.apiKey}` },
    payload: {
      model: "openrouter-3o",
      messages: [{ role: "user", content: "ask tool" }],
      client_metadata: { session_id: "tool-session" },
      tools: [{ type: "function", function: { name: "lookup", parameters: { type: "object", properties: {} } } }],
    },
  });
  assert.equal(toolRequest.statusCode, 200);
  const toolMessage = (toolRequest.json() as {
    choices: Array<{ message: { tool_calls: Array<{ id: string; function: { name: string; arguments: string } }> } }>;
  }).choices[0]!.message;
  const syntheticId = toolMessage.tool_calls[0]!.id;
  assert.match(syntheticId, /^call_[0-9a-f]+$/);

  const toolResult = await app.inject({
    method: "POST",
    url: "/v1/chat/completions",
    headers: { authorization: `Bearer ${config.apiKey}` },
    payload: {
      model: "openrouter-3o",
      client_metadata: { session_id: "tool-session" },
      messages: [
        { role: "user", content: "ask tool" },
        { role: "assistant", content: null, tool_calls: [{ id: syntheticId, type: "function", function: { name: "lookup", arguments: "{}" } }] },
        { role: "tool", tool_call_id: syntheticId, content: "done" },
      ],
    },
  });
  assert.equal(toolResult.statusCode, 200);

  const toolBodies = upstreamBodies.slice(7, 9);
  assert.notEqual(toolBodies[0]!["conversation_id"], toolBodies[1]!["conversation_id"]);
  const firstToolMessages = toolBodies[0]!["messages"] as Array<{ content: Array<Record<string, unknown>> }>;
  const resumedToolMessages = toolBodies[1]!["messages"] as Array<{ role: string; content: Array<Record<string, unknown>> }>;
  assert.equal(firstToolMessages[0]!.content[0]!["cache_control"], undefined);
  const resumedUser = resumedToolMessages.find((message) => message.role === "user")!;
  assert.equal(resumedUser.content[0]!["cache_control"], undefined);
});

test("空 messages/input 与非数值 max_tokens 在本地 400，不转发上游", async (t) => {
  const catalog = new CatalogStore();
  catalog.set([{
    id: "openrouter-3o",
    config: "openrouter-3o",
    model: "openrouter-3o__dev",
    contextWindow: 168000,
    usageLimited: false,
    repoRestricted: false,
    superModel: false,
    feeLevel: 0,
    manualUsage: 0,
  }]);

  let forwarded = 0;
  const fakeClient = {
    async chat(): Promise<Response> {
      forwarded += 1;
      throw new Error("upstream must not be called for invalid local requests");
    },
    async getAccountUsage(): Promise<never> {
      throw new Error("quota must not be requested in this test");
    },
  } as unknown as UpstreamClient;

  const app = buildApp({
    config,
    client: fakeClient,
    catalog,
    quota: new QuotaCache(fakeClient, catalog, new UsageStore()),
    usage: new UsageStore(),
  });
  t.after(async () => app.close());

  const anthHeaders = { "x-api-key": config.apiKey, "anthropic-version": "2023-06-01" };
  const openaiHeaders = { authorization: `Bearer ${config.apiKey}` };
  const cases: Array<{ url: string; headers: Record<string, string>; payload: Record<string, unknown> }> = [
    { url: "/v1/messages", headers: anthHeaders, payload: { model: "openrouter-3o", max_tokens: 16, messages: [] } },
    { url: "/v1/messages", headers: anthHeaders, payload: { model: "openrouter-3o", max_tokens: 16 } },
    {
      url: "/v1/messages",
      headers: anthHeaders,
      payload: { model: "openrouter-3o", system: "rules", max_tokens: 16, messages: [] },
    },
    {
      url: "/v1/messages",
      headers: anthHeaders,
      payload: { model: "openrouter-3o", max_tokens: "abc", messages: [{ role: "user", content: "hi" }] },
    },
    { url: "/v1/chat/completions", headers: openaiHeaders, payload: { messages: [] } },
    { url: "/v1/chat/completions", headers: openaiHeaders, payload: { model: "openrouter-3o" } },
    {
      url: "/v1/chat/completions",
      headers: openaiHeaders,
      payload: { messages: [{ role: "user", content: "hi" }], max_tokens: "abc" },
    },
    {
      url: "/v1/chat/completions",
      headers: openaiHeaders,
      payload: { messages: [{ role: "user", content: "hi" }], max_completion_tokens: 0 },
    },
    { url: "/v1/responses", headers: openaiHeaders, payload: { model: "openrouter-3o" } },
    { url: "/v1/responses", headers: openaiHeaders, payload: { input: "" } },
    { url: "/v1/responses", headers: openaiHeaders, payload: { input: [] } },
    { url: "/v1/responses", headers: openaiHeaders, payload: { instructions: "rules", input: [] } },
    {
      url: "/v1/responses",
      headers: openaiHeaders,
      payload: { input: "hi", max_output_tokens: "abc" },
    },
  ];

  for (const c of cases) {
    const res = await app.inject({ method: "POST", url: c.url, headers: c.headers, payload: c.payload });
    assert.equal(res.statusCode, 400, `${c.url} ${JSON.stringify(c.payload)}`);
    const json = res.json() as Record<string, unknown>;
    if (c.url === "/v1/messages") {
      assert.equal(json["type"], "error", c.url);
      assert.equal((json["error"] as Record<string, unknown>)["type"], "invalid_request_error");
    } else {
      assert.equal((json["error"] as Record<string, unknown>)["type"], "invalid_request_error", c.url);
    }
  }
  assert.equal(forwarded, 0, "非法请求不得转发上游");
});
