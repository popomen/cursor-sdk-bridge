import { strict as assert } from "node:assert";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { buildApp } from "../src/app.js";
import { BridgeRuntime, DEFAULT_MODEL } from "../src/bridge-runtime.js";
import { loadAdmissionConfig, type Config } from "../src/config.js";
import { CatalogStore } from "../src/upstream/catalog.js";
import type { UpstreamClient } from "../src/upstream/client.js";
import { QuotaCache } from "../src/upstream/quota.js";
import { UsageStore } from "../src/upstream/usage.js";

const config: Config = {
  host: "127.0.0.1", port: 8792, apiKey: "fixture-only", defaultModel: DEFAULT_MODEL,
  upstreamBaseUrl: "https://example.invalid", catalogBaseUrl: "https://example.invalid", traePpeEnv: "",
  credentialSource: "environment", traeToken: "fixture-token", traeTokenFile: "", traeTokenCommand: "",
  bytedcliBin: "", tokenCommandTimeoutMs: 1000, stateDir: "/tmp/unused",
  maxRequestBytes: 10000, upstreamHeaderTimeoutMs: 1000, streamIdleTimeoutMs: 1000,
  responsesDeferLifecycle: false, logLevel: "silent", logRequests: false, dumpBody: false, trace: false,
  admission: { ...loadAdmissionConfig({}), initialDelayMs: 1, maxDelayMs: 2, waitTimeoutMs: 1000 },
};
const headers = { authorization: "Bearer fixture-only" };
const success = () => new Response('event: output\ndata: {"response":"PRIVATE_OUTPUT"}\n\nevent: done\ndata: {"finish_reason":"stop"}\n\n');

function fixture(chat: (body: unknown, signal?: AbortSignal) => Promise<Response> = async () => success(), logDir?: string) {
  const bridge = new BridgeRuntime("a".repeat(40), logDir);
  const catalog = new CatalogStore();
  catalog.set(["GPT-6-Astra", DEFAULT_MODEL].map(id => ({ id, config: id, model: id, contextWindow: 1000000,
    usageLimited: true, repoRestricted: false, superModel: false, feeLevel: 0, manualUsage: 0,
    reasoning: { supportedEfforts: ["low", "medium", "high", "xhigh"], defaultEffort: "medium" } })));
  let calls = 0;
  const client = { chat: async (...args: Parameters<typeof chat>) => { calls++; return chat(...args); },
    getAccountUsage: async () => { throw new Error("quota unavailable"); },
    principalScope: async () => "fixture-principal" } as unknown as UpstreamClient;
  const usage = new UsageStore();
  const app = buildApp({ config, client, catalog, usage, quota: new QuotaCache(client, catalog, usage), bridge });
  return { app, bridge, calls: () => calls };
}

test("managed service only admits Astra Responses and reports its catalog in both formats", async t => {
  const f = fixture(); t.after(() => f.app.close());
  for (const url of ["/v1/messages", "/v1/chat/completions", "/v1/model-status", "/"]) {
    assert.equal((await f.app.inject({ method: "POST", url, headers, payload: {} })).statusCode, 404);
  }
  for (const model of ["typo", "claude-opus-5-5-high", "gpt-6", "DeepSeek-V4-Pro"]) {
    assert.equal((await f.app.inject({ method: "POST", url: "/v1/responses", headers,
      payload: { model, input: "private prompt" } })).statusCode, 400);
  }
  assert.equal(f.calls(), 0);
  assert.equal(f.bridge.runs.size, 0);
  const list = (await f.app.inject({ url: "/v1/models", headers })).json();
  const codex = (await f.app.inject({ url: "/models?client_version=0.155.1", headers })).json();
  assert.deepEqual(list.data.map((m: { id: string }) => m.id), codex.models.map((m: { slug: string }) => m.slug));
  assert.deepEqual(codex.models[1].supported_reasoning_levels.map((e: { effort: string }) => e.effort), ["low", "medium", "high", "xhigh"]);
});

test("strict reasoning validation, required authentication and no saved response lookup", async t => {
  const f = fixture(); t.after(() => f.app.close());
  assert.equal((await f.app.inject({ method: "POST", url: "/v1/responses", payload: { input: "x" } })).statusCode, 401);
  for (const extra of [{ reasoning: { effort: "ultra" } }, { previous_response_id: "private-id" }]) {
    assert.equal((await f.app.inject({ method: "POST", url: "/v1/responses", headers,
      payload: { model: DEFAULT_MODEL, input: "x", ...extra } })).statusCode, 400);
  }
  assert.equal(f.calls(), 0);
});

test("drain refuses unfinished inference, then closes admission until resume", async t => {
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const f = fixture(async () => { await waiting; return success(); });
  t.after(() => f.app.close());
  const pending = f.app.inject({ method: "POST", url: "/v1/responses", headers,
    payload: { model: DEFAULT_MODEL, input: "private prompt" } }).then(r => r);
  while (!f.calls()) await new Promise(resolve => setImmediate(resolve));
  const health = (await f.app.inject({ url: "/health" })).json();
  assert.equal(health.unfinished, 1);
  assert.equal(health.service, "traex-bridge");
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain", headers })).statusCode, 409);
  assert.equal(f.bridge.draining, false);
  release(); await pending;
  assert.equal(f.bridge.runs.size, 0);
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain", headers })).json().draining, true);
  assert.equal((await f.app.inject({ method: "POST", url: "/v1/responses", headers, payload: { input: "x" } })).statusCode, 503);
  await f.app.inject({ method: "POST", url: "/admin/resume", headers });
  assert.equal(f.bridge.draining, false);
});

test("administration requires the key and rejects browser origins and foreign hosts", async t => {
  const f = fixture(); t.after(() => f.app.close());
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain" })).statusCode, 401);
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain", headers: { ...headers, origin: "https://example.invalid" } })).statusCode, 403);
  assert.equal((await f.app.inject({ url: "/health", headers: { host: "evil.invalid" } })).statusCode, 403);
  assert.equal(f.bridge.draining, false);
});

test("completed SSE releases admission and logs no prompt, output, key or tool data", async t => {
  const logDir = mkdtempSync(join(tmpdir(), "traex-metadata-"));
  const f = fixture(undefined, logDir);
  t.after(async () => { await f.app.close(); rmSync(logDir, { recursive: true, force: true }); });
  const result = await f.app.inject({ method: "POST", url: "/v1/responses", headers,
    payload: { model: DEFAULT_MODEL, input: "PRIVATE_PROMPT", stream: true } });
  assert.match(result.body, /response.completed/);
  assert.equal(f.bridge.runs.size, 0);
  const log = readFileSync(join(logDir, "requests.jsonl"), "utf8");
  assert.doesNotMatch(log, /PRIVATE_|fixture-only|fixture-token/);
  const entries = log.trim().split("\n").map(l => JSON.parse(l));
  assert.equal(entries.filter(e => e.event === "inference").length, 1);
  assert.equal(entries[0].outcome, "completed");
});

test("upstream errors release resources without leaking raw errors to metadata", async t => {
  const f = fixture(async () => { throw new Error("PRIVATE_TOKEN_AND_BODY"); });
  t.after(() => f.app.close());
  const result = await f.app.inject({ method: "POST", url: "/v1/responses", headers,
    payload: { model: DEFAULT_MODEL, input: "private prompt" } });
  assert.equal(result.statusCode, 502);
  assert.equal(f.bridge.runs.size, 0);
});

test("client disconnect aborts upstream and remains unfinished until upstream cleanup settles", async t => {
  let aborted = false;
  let release!: () => void;
  const cleanup = new Promise<void>(resolve => { release = resolve; });
  const f = fixture(async (_body, signal) => {
    await new Promise<void>(resolve => signal!.addEventListener("abort", () => { aborted = true; resolve(); }, { once: true }));
    await cleanup;
    throw new Error("upstream canceled");
  });
  t.after(() => f.app.close());
  await f.app.listen({ host: "127.0.0.1", port: 0 });
  const address = f.app.server.address() as { port: number };
  const controller = new AbortController();
  const response = fetch(`http://127.0.0.1:${address.port}/v1/responses`, {
    method: "POST", headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify({ model: DEFAULT_MODEL, input: "fixture" }), signal: controller.signal,
  }).catch(() => null);
  while (!f.calls()) await new Promise(resolve => setImmediate(resolve));
  controller.abort();
  await response;
  while (!aborted) await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.bridge.runs.size, 1);
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain", headers })).statusCode, 409);
  release();
  while (f.bridge.runs.size) await new Promise(resolve => setImmediate(resolve));
  assert.equal((await f.app.inject({ method: "POST", url: "/admin/drain", headers })).statusCode, 200);
});
