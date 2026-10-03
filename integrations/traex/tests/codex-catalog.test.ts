import { strict as assert } from "node:assert";
import { test } from "node:test";

import { buildApp } from "../src/app.js";
import type { Config } from "../src/config.js";
import { buildCodexModelsResponse } from "../src/models/codex-catalog.js";
import codexTemplate from "../src/models/codex-template.json";
import { CatalogStore, type CatalogModel } from "../src/upstream/catalog.js";
import type { UpstreamClient } from "../src/upstream/client.js";
import { QuotaCache } from "../src/upstream/quota.js";
import { UsageStore } from "../src/upstream/usage.js";

const models: CatalogModel[] = [
  {
    id: "GPT-5.6-Sol[1m]",
    config: "gpt-5.6-sol",
    model: "gpt-5.6-sol__max",
    contextWindow: 936000,
    usageLimited: false,
    repoRestricted: false,
    superModel: false,
    feeLevel: 0,
    manualUsage: 0,
    reasoning: {
      supportedEfforts: ["low", "HIGH", "high", "unsupported"],
      defaultEffort: "xhigh",
    },
  },
  {
    id: "Seed-2.1-Pro",
    config: "seed-2.1-pro",
    model: "seed-2.1-pro",
    contextWindow: 184000,
    usageLimited: false,
    repoRestricted: false,
    superModel: false,
    feeLevel: 0,
    manualUsage: 0,
    reasoning: { supportedEfforts: [], defaultEffort: "high" },
  },
];

test("Codex 目录继承模板并覆盖 Traex 实时 metadata", () => {
  const response = buildCodexModelsResponse(models);
  assert.equal(response.models.length, 2);

  const gpt = response.models[0]!;
  assert.equal(gpt["slug"], "GPT-5.6-Sol[1m]");
  assert.equal(gpt["context_window"], 936000);
  assert.equal(gpt["max_context_window"], 936000);
  assert.equal(gpt["default_reasoning_level"], "xhigh");
  assert.deepEqual(gpt["supported_reasoning_levels"], [
    { effort: "low", description: "Fast responses with lighter reasoning" },
    { effort: "high", description: "Greater reasoning depth for complex problems" },
  ]);
  assert.equal(gpt["priority"], 1);
  assert.equal(gpt["prefer_websockets"], false);
  assert.equal(gpt["use_responses_lite"], false);
  assert.equal(gpt["multi_agent_version"], null);
  assert.equal(gpt["tool_mode"], "direct");
  assert.equal(gpt["supports_search_tool"], false);
  assert.equal(gpt["supports_reasoning_summaries"], true);
  assert.equal(gpt["supports_image_detail_original"], false);
  assert.equal(gpt["web_search_tool_type"], "text");
  assert.equal(gpt["support_verbosity"], false);
  assert.equal(gpt["supports_parallel_tool_calls"], true);
  assert.ok(String(gpt["base_instructions"]).length > 10_000);

  const seed = response.models[1]!;
  assert.equal(seed["default_reasoning_level"], "high");
  assert.deepEqual(seed["supported_reasoning_levels"], []);
});

test("Codex 目录包含客户端要求的必填字段并为非推理模型关闭摘要", () => {
  const nonReasoning: CatalogModel = {
    ...models[1]!,
    id: "Qwen3.6-Plus",
    config: "qwen-3.6-plus",
    model: "qwen-3.6-plus__dev",
    reasoning: undefined,
  };
  const response = buildCodexModelsResponse([models[0]!, nonReasoning]);
  const required = [
    "slug",
    "supported_reasoning_levels",
    "shell_type",
    "visibility",
    "supported_in_api",
    "priority",
    "base_instructions",
    "supports_reasoning_summaries",
    "support_verbosity",
    "truncation_policy",
    "supports_parallel_tool_calls",
    "experimental_supported_tools",
  ];
  for (const entry of response.models) {
    for (const field of required) assert.ok(Object.hasOwn(entry, field), `${field} must be present`);
  }
  assert.equal(response.models[0]!["supports_reasoning_summaries"], true);
  assert.equal(response.models[1]!["supports_reasoning_summaries"], false);
  assert.equal(response.models[0]!["supports_image_detail_original"], false);
  assert.equal(response.models[0]!["web_search_tool_type"], "text");

  assert.equal(codexTemplate.supports_reasoning_summaries, false);
  assert.equal(codexTemplate.supports_image_detail_original, false);
  assert.equal(codexTemplate.web_search_tool_type, "text");
});

const config: Config = {
  host: "127.0.0.1",
  port: 8317,
  apiKey: "test-key",
  defaultModel: "GPT-5.6-Sol[1m]",
  upstreamBaseUrl: "https://example.invalid",
  catalogBaseUrl: "https://example.invalid",
  traePpeEnv: "",
  credentialSource: "environment",
  traeToken: "test-token",
  traeTokenFile: "",
  traeTokenCommand: "",
  bytedcliBin: "",
  tokenCommandTimeoutMs: 1_000,
  stateDir: "/tmp/traex-bridge-codex-catalog-test",
  maxRequestBytes: 1_000_000,
  upstreamHeaderTimeoutMs: 5_000,
  streamIdleTimeoutMs: 5_000,
  responsesDeferLifecycle: false,
  logLevel: "silent",
  logRequests: false,
  dumpBody: false,
  trace: false,
};

test("/v1/models 只对 Codex client_version 切换响应协议", async (t) => {
  const catalog = new CatalogStore();
  catalog.set(models);
  const client = {} as UpstreamClient;
  const usage = new UsageStore();
  const app = buildApp({
    config,
    client,
    catalog,
    quota: new QuotaCache(client, catalog, usage),
    usage,
  });
  t.after(async () => app.close());

  const standard = await app.inject({
    method: "GET",
    url: "/v1/models",
    headers: { authorization: `Bearer ${config.apiKey}` },
  });
  assert.equal(standard.statusCode, 200);
  const standardBody = standard.json() as Record<string, unknown>;
  assert.equal(standardBody["object"], "list");
  assert.equal((standardBody["data"] as unknown[]).length, 2);
  assert.equal(standardBody["models"], undefined);

  const codex = await app.inject({
    method: "GET",
    url: "/v1/models?client_version=0.147.0",
    headers: { authorization: `Bearer ${config.apiKey}` },
  });
  assert.equal(codex.statusCode, 200);
  const codexBody = codex.json() as Record<string, unknown>;
  assert.equal(codexBody["object"], undefined);
  assert.equal(codexBody["data"], undefined);
  const codexModels = codexBody["models"] as Array<Record<string, unknown>>;
  assert.equal(codexModels.length, 2);
  assert.deepEqual(codexModels.map((model) => model["slug"]), ["GPT-5.6-Sol[1m]", "Seed-2.1-Pro"]);
});
