import { strict as assert } from "node:assert";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";

import { buildApp } from "../src/app.js";
import type { Config } from "../src/config.js";
import { ModelStatusService } from "../src/model-status.js";
import { CatalogStore } from "../src/upstream/catalog.js";
import type { UpstreamClient } from "../src/upstream/client.js";
import { QuotaCache, type QuotaSnapshot } from "../src/upstream/quota.js";
import { UsageStore } from "../src/upstream/usage.js";

const config: Config = {
  host: "127.0.0.1",
  port: 8317,
  apiKey: "dashboard-key",
  defaultModel: "seed",
  upstreamBaseUrl: "https://example.invalid",
  catalogBaseUrl: "https://example.invalid",
  traePpeEnv: "",
  credentialSource: "environment",
  traeToken: "test-token",
  traeTokenFile: "",
  traeTokenCommand: "",
  bytedcliBin: "",
  tokenCommandTimeoutMs: 1_000,
  stateDir: "/tmp/traex-bridge-dashboard-test",
  maxRequestBytes: 1_000_000,
  upstreamHeaderTimeoutMs: 5_000,
  streamIdleTimeoutMs: 5_000,
  responsesDeferLifecycle: false,
  logLevel: "silent",
  logRequests: false,
  dumpBody: false,
  trace: false,
};

function detailResponse(load = 42): unknown {
  return {
    config_info_list: [
      {
        config_name: "seed",
        usage: "chat_completion",
        config_switch: true,
        display_config: {
          display_name: "Seed-Test",
          hot_info: { hot: load },
          is_internal_usage_limit: true,
        },
        model_detail_list: [{ model_name: "seed__dev" }, { model_name: "seed__max" }],
      },
    ],
  };
}

function quotaSnapshot(percent = 75): QuotaSnapshot {
  return {
    quota: {
      pack: "hidden",
      resets_at: 0,
      dollar_billing: false,
      pools: [{ name: "basic", limit: 300, used: 225, remaining: 75, percent, depleted: false, unlimited: false }],
    },
    models: [],
  };
}

test("根页面与只读状态接口均无需 API Key", async (t) => {
  const client = {
    async getDetailParam(): Promise<unknown> { return detailResponse(); },
  } as unknown as UpstreamClient;
  const quota = { snapshot: (): QuotaSnapshot => quotaSnapshot() } as QuotaCache;
  const app = buildApp({ config, client, catalog: new CatalogStore(), quota, usage: new UsageStore() });
  t.after(async () => app.close());

  const page = await app.inject({ method: "GET", url: "/" });
  assert.equal(page.statusCode, 200);
  assert.match(page.headers["content-type"] ?? "", /text\/html/);
  assert.equal(page.headers["cache-control"], "no-store");
  assert.match(page.headers["content-security-policy"] ?? "", /connect-src 'self'/);
  assert.doesNotMatch(page.body, /dashboard-key/);
  assert.match(page.body, /\/v1\/model-status/);
  assert.match(page.body, /\.list\{columns:2;column-gap:12px\}/);
  assert.match(page.body, /@media\(max-width:760px\).*\.list\{columns:1\}/);
  assert.match(page.body, /prefers-color-scheme:dark/);
  assert.match(page.body, /traex-dashboard-theme/);
  assert.doesNotMatch(page.body, /sessionStorage|Authorization|API Key/);

  const crossOrigin = await app.inject({
    method: "GET",
    url: "/v1/model-status",
    headers: { host: "bridge.example", origin: "https://example.com" },
  });
  assert.equal(crossOrigin.statusCode, 200);
  assert.equal(crossOrigin.headers["access-control-allow-origin"], "*");

  const protectedModels = await app.inject({ method: "GET", url: "/v1/models" });
  assert.equal(protectedModels.statusCode, 401);

  const response = await app.inject({ method: "GET", url: "/v1/model-status" });
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers["cache-control"], "no-store");
  const data = response.json();
  assert.equal(data.models.length, 1);
  assert.deepEqual(data.models[0], {
    id: "seed",
    name: "Seed-Test",
    load: 42,
    limited: true,
    max: true,
    selectable: true,
    icon: "https://lf-cdn.trae.com.cn/obj/trae-com-cn/model/doubao.svg",
    quotaPercent: 75,
  });
});

test("ModelStatusService 合并并发请求并在 TTL 内复用快照", async () => {
  let calls = 0;
  let now = 1_000;
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const client = {
    async getDetailParam(_signal?: AbortSignal, fn?: string): Promise<unknown> {
      calls += 1;
      assert.equal(fn, "solo_agent");
      await gate;
      return detailResponse(0);
    },
  } as unknown as UpstreamClient;
  const quota = { snapshot: (): QuotaSnapshot => quotaSnapshot(30) } as QuotaCache;
  const service = new ModelStatusService(client, quota, () => now);

  const first = service.get();
  const second = service.get();
  assert.equal(calls, 1);
  release?.();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.models[0]?.load, 0);
  assert.deepEqual(a, b);

  now += 10_000;
  await service.get();
  assert.equal(calls, 1);
});

test("ModelStatusService does not wait for an uncached quota refresh", async () => {
  let quotaCalls = 0;
  const client = {
    async getDetailParam(): Promise<unknown> { return detailResponse(12); },
  } as unknown as UpstreamClient;
  const quota = {
    snapshot: (): QuotaSnapshot | null => null,
    get: async (): Promise<QuotaSnapshot> => {
      quotaCalls += 1;
      return await new Promise<QuotaSnapshot>(() => {});
    },
  } as QuotaCache;
  const service = new ModelStatusService(client, quota);

  const snapshot = await Promise.race([
    service.get(),
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error("model status blocked on quota")), 100)),
  ]);

  assert.equal(snapshot.models[0]?.load, 12);
  assert.equal(snapshot.models[0]?.quotaPercent, null);
  assert.equal(quotaCalls, 0);
});

test("ModelStatusService picks up quota that arrives during the catalog fetch", async () => {
  const catalogStore = new CatalogStore();
  const usage = new UsageStore();
  let quota!: QuotaCache;
  const client = {
    async getAccountUsage(): Promise<unknown> {
      return {
        user_entitlement_pack_list: [{
          entitlement_base_info: { product_type: 0, entitlement_id: "free-pack" },
          quota: { basic_usage_limit: 100 },
          usage: { basic_usage_amount: 75 },
        }],
      };
    },
    async getDetailParam(): Promise<unknown> {
      const deadline = Date.now() + 500;
      while (quota.snapshot() === null) {
        if (Date.now() > deadline) throw new Error("quota did not populate during catalog wait");
        await delay(5);
      }
      return detailResponse(12);
    },
  } as unknown as UpstreamClient;
  quota = new QuotaCache(client, catalogStore, usage);
  const service = new ModelStatusService(client, quota);
  const snapshot = await service.get();
  assert.equal(snapshot.models[0]?.load, 12);
  assert.equal(snapshot.models[0]?.quotaPercent, 75);
});

test("ModelStatusService 失败后退避并返回 stale 快照", async () => {
  let calls = 0;
  let now = 1_000;
  const client = {
    async getDetailParam(): Promise<unknown> {
      calls += 1;
      if (calls > 1) throw new Error("upstream unavailable");
      return detailResponse(55);
    },
  } as unknown as UpstreamClient;
  const quota = { snapshot: (): QuotaSnapshot => quotaSnapshot() } as QuotaCache;
  const service = new ModelStatusService(client, quota, () => now);

  await service.get();
  now += 16_000;
  const stale = await service.get();
  assert.equal(stale.stale, true);
  assert.equal(calls, 2);

  now += 1_000;
  const backedOff = await service.get();
  assert.equal(backedOff.stale, true);
  assert.equal(calls, 2);
});

test("ModelStatusService 关闭时取消目录请求", async () => {
  let signal: AbortSignal | undefined;
  const client = {
    async getDetailParam(value?: AbortSignal): Promise<unknown> {
      signal = value;
      return new Promise((_resolve, reject) => value?.addEventListener("abort", () => reject(value.reason), { once: true }));
    },
  } as unknown as UpstreamClient;
  const quota = { snapshot: (): QuotaSnapshot => quotaSnapshot() } as QuotaCache;
  const service = new ModelStatusService(client, quota);
  const pending = service.get();
  service.close();
  await assert.rejects(pending, /closed/);
  assert.equal(signal?.aborted, true);
});
