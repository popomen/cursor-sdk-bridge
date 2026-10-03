/**
 * 账号额度（`ide_user_ent_usage`）。
 *
 * 上游的 `get_detail_param` 里 `display_config.internal_usage` 永远是 null，
 * 拿不到任何实时用量 —— TRAE 客户端自己的做法是把共享的 `basic` 池投影到每个
 * metered 模型上。这里照做。
 *
 * 注意 `basic_usage_amount` **滞后真实限流数分钟**：一个模型可能已经被上游拒绝
 * 而池子数字还显示有余量。所以要叠加本进程的实时观测（UsageStore）。
 */

import type { UpstreamClient } from "./client.js";
import type { CatalogStore } from "./catalog.js";
import type { UsageStore } from "./usage.js";

/** 缓存很短 —— 账号数字本来就滞后，缓存久了雪上加霜 */
export const QUOTA_TTL_MS = 20_000;

export interface QuotaPool {
  name: string;
  limit: number;
  used: number;
  remaining?: number;
  percent?: number;
  depleted: boolean;
  unlimited: boolean;
}

export interface QuotaInfo {
  pack: string;
  resets_at: number;
  dollar_billing: boolean;
  pools: QuotaPool[];
}

export interface ModelQuota {
  model: string;
  limit: number;
  used: number;
  remaining: number;
  percent: number;
  depleted: boolean;
  live_limited: boolean;
  resets_at: number;
}

export interface QuotaSnapshot {
  quota: QuotaInfo;
  models: ModelQuota[];
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

const POOL_FIELDS: ReadonlyArray<readonly [string, readonly [string, string], readonly [string, string]]> = [
  ["basic", ["basic_usage_limit", "basicUsageLimit"], ["basic_usage_amount", "basicUsageAmount"]],
  ["advanced", ["advanced_model_request_limit", "advancedModelRequestLimit"], ["advanced_model_request_usage", "advancedModelRequestUsage"]],
  ["premium_fast", ["premium_model_fast_request_limit", "premiumModelFastRequestLimit"], ["premium_model_fast_request_usage", "premiumModelFastRequestUsage"]],
  ["premium_slow", ["premium_model_slow_request_limit", "premiumModelSlowRequestLimit"], ["premium_model_slow_request_usage", "premiumModelSlowRequestUsage"]],
  ["auto_completion", ["auto_completion_limit", "autoCompletionLimit"], ["auto_completion_usage", "autoCompletionUsage"]],
  ["bonus", ["bonus_usage_limit", "bonusUsageLimit"], ["bonus_usage_amount", "bonusUsageAmount"]],
];

function valueAt(obj: Record<string, unknown>, [snake, camel]: readonly [string, string]): unknown {
  return obj[snake] ?? obj[camel];
}

function objectAt(obj: Record<string, unknown>, keys: readonly [string, string]): Record<string, unknown> {
  const value = valueAt(obj, keys);
  return isPlainObject(value) ? value : {};
}

function firstNumber(
  objects: readonly Record<string, unknown>[],
  keys: readonly [string, string],
): number {
  for (const obj of objects) {
    const value = valueAt(obj, keys);
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return 0;
}

function makePool(name: string, limit: number, used: number): QuotaPool | null {
  // 该账号不适用的池子直接跳过
  if (limit === 0 && used === 0) return null;

  if (limit < 0) {
    return { name, limit, used, depleted: false, unlimited: true };
  }
  if (limit === 0) {
    // 零额度上的超额
    return { name, limit, used, remaining: 0, percent: 100, depleted: true, unlimited: false };
  }
  const remaining = Math.max(0, limit - used);
  // 百分比四舍五入并限制在 0..100；超额靠 depleted 表达而不是 >100%。
  const percent = Math.max(0, Math.min(100, Math.round((used / limit) * 100)));
  return { name, limit, used, remaining, percent, depleted: used >= limit, unlimited: false };
}

export function parseAccountQuota(resp: unknown): QuotaInfo {
  if (!isPlainObject(resp)) throw new Error("ide_user_ent_usage: response is not an object");
  const data = isPlainObject(resp["data"]) ? resp["data"] : {};
  const packs = resp["user_entitlement_pack_list"]
    ?? data["user_entitlement_pack_list"]
    ?? resp["userEntitlementPackList"]
    ?? data["userEntitlementPackList"];
  if (!Array.isArray(packs) || packs.length === 0) {
    throw new Error("ide_user_ent_usage: empty user_entitlement_pack_list");
  }

  const chosen = packs.find((pack) => {
    if (!isPlainObject(pack)) return false;
    const base = objectAt(pack, ["entitlement_base_info", "entitlementBaseInfo"]);
    const productType = valueAt(base, ["product_type", "productType"]);
    return productType === 0
      || (typeof productType === "string" && productType.toLowerCase() === "free");
  });
  if (!isPlainObject(chosen)) throw new Error("ide_user_ent_usage: free entitlement pack not found");

  const base = objectAt(chosen, ["entitlement_base_info", "entitlementBaseInfo"]);
  const usageObj = isPlainObject(chosen["usage"]) ? chosen["usage"] : {};
  const productExtra = objectAt(base, ["product_extra", "productExtra"]);
  const subscriptionExtra = objectAt(productExtra, ["subscription_extra", "subscriptionExtra"]);
  const limitSources = [
    objectAt(chosen, ["quota", "quota"]),
    objectAt(base, ["quota", "quota"]),
    objectAt(subscriptionExtra, ["quota", "quota"]),
  ];

  const pools: QuotaPool[] = [];
  for (const [name, limitKeys, usageKeys] of POOL_FIELDS) {
    const pool = makePool(name, firstNumber(limitSources, limitKeys), num(valueAt(usageObj, usageKeys)));
    if (pool) pools.push(pool);
  }

  const dollarBilling = resp["is_dollar_usage_billing"]
    ?? data["is_dollar_usage_billing"]
    ?? resp["isDollarUsageBilling"]
    ?? data["isDollarUsageBilling"];
  return {
    pack: typeof valueAt(base, ["entitlement_id", "entitlementId"]) === "string"
      ? valueAt(base, ["entitlement_id", "entitlementId"]) as string
      : "",
    resets_at: num(valueAt(base, ["end_time", "endTime"])),
    dollar_billing: dollarBilling === true,
    pools,
  };
}

export class QuotaCache {
  private cached: { info: QuotaInfo; at: number } | null = null;
  private inflight: Promise<QuotaInfo> | null = null;

  constructor(
    private readonly client: UpstreamClient,
    private readonly catalog: CatalogStore,
    private readonly usage: UsageStore,
    private readonly logger?: { warn(...a: unknown[]): void },
  ) {}

  /** 可阻塞版本：无缓存时同步拉取 */
  async get(): Promise<QuotaSnapshot> {
    const info = await this.getInfo();
    return { quota: info, models: this.project(info) };
  }

  /** 非阻塞版本：缺失或过期只触发后台刷新，供 /v1/models 热路径用 */
  snapshot(): QuotaSnapshot | null {
    const c = this.cached;
    if (!c) {
      void this.refreshInBackground();
      return null;
    }
    if (Date.now() - c.at > QUOTA_TTL_MS) void this.refreshInBackground();
    return { quota: c.info, models: this.project(c.info) };
  }

  perModelSnapshot(upstreamModelName: string): ModelQuota | undefined {
    const snap = this.snapshot();
    if (!snap) return undefined;
    return snap.models.find((m) => m.model === upstreamModelName);
  }

  private async getInfo(): Promise<QuotaInfo> {
    const c = this.cached;
    if (c && Date.now() - c.at <= QUOTA_TTL_MS) return c.info;
    if (c) {
      void this.refreshInBackground(); // stale-while-revalidate
      return c.info;
    }
    return this.fetchOnce();
  }

  private refreshInBackground(): Promise<void> {
    return this.fetchOnce()
      .then(() => undefined)
      .catch((e: unknown) => {
        this.logger?.warn(`quota refresh failed: ${(e as Error).message}`);
      });
  }

  private fetchOnce(): Promise<QuotaInfo> {
    if (this.inflight) return this.inflight;
    const p = this.client
      .getAccountUsage()
      .then((resp) => {
        const info = parseAccountQuota(resp);
        this.cached = { info, at: Date.now() };
        return info;
      })
      .finally(() => {
        this.inflight = null;
      });
    this.inflight = p;
    return p;
  }

  /** 把共享的 basic 池投影到每个 metered 模型，叠加本进程实时观测 */
  private project(info: QuotaInfo): ModelQuota[] {
    const basic = info.pools.find((p) => p.name === "basic");
    if (!basic) return [];

    return this.catalog.meteredModelNames().map((model) => {
      const liveLimited = this.usage.isQuotaLimited(model);
      return {
        model,
        limit: basic.limit,
        used: basic.used,
        remaining: basic.remaining ?? 0,
        percent: basic.percent ?? 0,
        // 实时拒绝优先于滞后的池子数字
        depleted: basic.depleted || liveLimited,
        live_limited: liveLimited,
        resets_at: info.resets_at,
      };
    });
  }
}
