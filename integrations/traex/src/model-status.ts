import { parseCatalog, type CatalogModel } from "./upstream/catalog.js";
import type { UpstreamClient } from "./upstream/client.js";
import type { QuotaCache } from "./upstream/quota.js";

const STATUS_TTL_MS = 15_000;
const RETRY_BACKOFF_MS = 15_000;
const MAX_STALE_MS = 10 * 60_000;

const icons = {
  seed: "https://lf-cdn.trae.com.cn/obj/trae-com-cn/model/doubao.svg",
  openrouter: "https://openrouter.ai/favicon.ico",
  gpt: "https://lf-cdn.trae.com.cn/obj/trae-com-cn/model/openai.svg",
  deepseek: "https://lf-cdn.trae.com.cn/obj/trae-com-cn/model/deepseek.svg",
  gemini: "https://lf-cdn.trae.com.cn/obj/trae-com-cn/model/gemini.svg",
} as const;

export interface ModelStatusItem {
  id: string;
  name: string;
  load: number | null;
  limited: boolean;
  max: boolean;
  selectable: true;
  icon: string | null;
  quotaPercent: number | null;
}

export interface ModelStatusSnapshot {
  object: "trae.model_status";
  models: ModelStatusItem[];
  updatedAt: string;
  stale: boolean;
}

function iconFor(model: CatalogModel): string | null {
  const name = `${model.config} ${model.id}`.toLowerCase();
  if (name.includes("openrouter")) return icons.openrouter;
  if (name.includes("seed") || name.includes("doubao")) return icons.seed;
  if (name.includes("gpt")) return icons.gpt;
  if (name.includes("deepseek")) return icons.deepseek;
  if (name.includes("gemini")) return icons.gemini;
  return null;
}

export class ModelStatusService {
  private cached: { snapshot: ModelStatusSnapshot; at: number } | null = null;
  private inflight: Promise<ModelStatusSnapshot> | null = null;
  private lastAttemptAt = 0;
  private readonly abortController = new AbortController();

  constructor(
    private readonly client: UpstreamClient,
    private readonly quota: QuotaCache,
    private readonly now: () => number = Date.now,
  ) {}

  close(): void {
    this.abortController.abort(new Error("model status service closed"));
  }

  async get(): Promise<ModelStatusSnapshot> {
    const now = this.now();
    const cached = this.cached;
    if (cached && now - cached.at <= STATUS_TTL_MS) return cached.snapshot;
    if (this.inflight) return this.inflight;
    if (this.lastAttemptAt > 0 && now - this.lastAttemptAt < RETRY_BACKOFF_MS) {
      if (cached && now - cached.at <= MAX_STALE_MS) return { ...cached.snapshot, stale: true };
      throw new Error("model status refresh is backing off");
    }
    this.lastAttemptAt = now;

    const refresh = this.fetch()
      .then((snapshot) => {
        this.cached = { snapshot, at: this.now() };
        return snapshot;
      })
      .catch((error: unknown) => {
        const fallback = this.cached;
        if (fallback && this.now() - fallback.at <= MAX_STALE_MS) {
          return { ...fallback.snapshot, stale: true };
        }
        throw error;
      })
      .finally(() => {
        this.inflight = null;
      });
    this.inflight = refresh;
    return refresh;
  }

  private async fetch(): Promise<ModelStatusSnapshot> {
    // Kick a background fill; the value is sampled again after catalog returns.
    this.quota.snapshot();
    const rawCatalog = await this.client.getDetailParam(this.abortController.signal, "solo_agent");
    const quotaSnapshot = this.quota.snapshot();
    const variantsByConfig = new Map<string, CatalogModel[]>();
    for (const model of parseCatalog(rawCatalog)) {
      const variants = variantsByConfig.get(model.config) ?? [];
      variants.push(model);
      variantsByConfig.set(model.config, variants);
    }

    const basic = quotaSnapshot?.quota.pools.find((pool) => pool.name === "basic");
    const weeklyPercent = basic?.unlimited === false ? basic.percent ?? null : null;
    const models = [...variantsByConfig.entries()].map(([config, variants]) => {
      const model = variants.find((variant) => variant.model.endsWith("__dev")) ?? variants[0]!;
      return {
        id: config,
        name: model.id.replace(/\[1m\]$/, ""),
        load: model.load ?? variants.find((variant) => variant.load !== undefined)?.load ?? null,
        limited: model.usageLimited,
        max: variants.some((variant) => variant.model.endsWith("__max")),
        selectable: true as const,
        icon: iconFor(model),
        quotaPercent: model.usageLimited ? weeklyPercent : null,
      };
    });

    return {
      object: "trae.model_status",
      models,
      updatedAt: new Date(this.now()).toISOString(),
      stale: false,
    };
  }
}
