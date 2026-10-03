/**
 * 实时模型目录（`get_detail_param`）。
 *
 * 没有编译期 fallback 列表 —— 启动时必须成功拉一次，失败即启动失败。
 * 之后每 30 分钟刷新；刷新失败只打日志并保留上一份内存副本。
 */

import type { UpstreamClient } from "./client.js";

export const CATALOG_TTL_MS = 30 * 60_000;

export interface ReasoningInfo {
  supportedEfforts: string[];
  defaultEffort: string;
}

export interface CatalogModel {
  /** 对外暴露的 model id，如 `GPT-5.6-Sol[1m]` */
  id: string;
  /** 上游 config_name，如 `gpt-5.6-sol` */
  config: string;
  /** 上游 model_name，如 `gpt-5.6-sol__max` */
  model: string;
  contextWindow: number;
  /** 该模型计入 metered 额度池 */
  usageLimited: boolean;
  repoRestricted: boolean;
  superModel: boolean;
  feeLevel: number;
  manualUsage: number;
  load?: number;
  reasoning?: ReasoningInfo;
}

const REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] as const;
const REASONING_DEFAULT_PREFERENCE = ["medium", "low", "high", "minimal", "xhigh", "max", "ultra", "none"] as const;
const DEFAULT_CONTEXT_WINDOW = 128_000;
const EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95;
const CODEX_DERIVED_CONTEXT_WINDOW_CAP = 800_000;

/**
 * 从上游 model_name 推导对外 id。
 *
 * `__max` 变体暴露成 `<base>[1m]` 而不是 `<base>-max`：方括号 affix 会被
 * oh-my-pi 之类的 harness 当作可剥离后缀，解析回 base 模型并继承其 reasoning
 * 元数据；而裸 `-max` 会和 reasoning-effort 词表冲突（`max` 本身是一个 effort 等级），
 * 导致大 context 变体丢失 effort 控制。
 */
export function variantId(base: string, modelName: string): string {
  const i = modelName.lastIndexOf("__");
  if (i >= 0) {
    const suffix = modelName.slice(i + 2);
    if (suffix === "" || suffix === "dev") return base; // 标准档保留 base id
    if (suffix === "max") return `${base}[1m]`;
    return `${base}-${suffix}`;
  }
  return base;
}

/**
 * `model_extra_config` 是被 JSON 编码过的字符串，要二次 parse。
 * 非推理模型的该字段可能缺失/为空/非法 —— 都返回 undefined，不抛异常。
 */
export function parseReasoningEfforts(raw: unknown): ReasoningInfo | undefined {
  if (typeof raw !== "string" || raw.trim() === "") return undefined;
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (typeof obj !== "object" || obj === null) return undefined;
  const o = obj as Record<string, unknown>;

  const normalize = (value: unknown): string => {
    if (typeof value !== "string") return "";
    const effort = value.trim().toLowerCase();
    return (REASONING_EFFORTS as readonly string[]).includes(effort) ? effort : "";
  };
  const thinking = (typeof o["Thinking"] === "object" && o["Thinking"] !== null
    ? o["Thinking"]
    : typeof o["thinking"] === "object" && o["thinking"] !== null
      ? o["thinking"]
      : {}) as Record<string, unknown>;
  const defaultCandidates = [
    normalize(o["ReasoningEffort"]),
    normalize(o["reasoning_effort"]),
    normalize(thinking["Type"] ?? thinking["type"]),
  ].filter((effort) => effort !== "");
  const optsRaw = o["reasoning_effort_options"];
  const supported = Array.isArray(optsRaw)
    ? [...new Set(optsRaw.map(normalize).filter((effort) => effort !== ""))]
        .sort((a, b) => REASONING_EFFORTS.indexOf(a as (typeof REASONING_EFFORTS)[number])
          - REASONING_EFFORTS.indexOf(b as (typeof REASONING_EFFORTS)[number]))
    : [];

  const defaultEffort = supported.length > 0
    ? defaultCandidates.find((effort) => supported.includes(effort))
      ?? REASONING_DEFAULT_PREFERENCE.find((effort) => supported.includes(effort))
      ?? supported[0]!
    : defaultCandidates[0] ?? "";

  if (supported.length === 0 && defaultEffort === "") return undefined;
  return { supportedEfforts: supported, defaultEffort };
}

function asNumber(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}
function asBoolean(v: unknown): boolean {
  return v === true;
}

function optionalNumber(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function contextWindow(promptMaxTokens: unknown, maxTokens: unknown): number {
  const prompt = optionalNumber(promptMaxTokens);
  const output = optionalNumber(maxTokens);
  if (prompt !== undefined && output !== undefined) return prompt + output;
  if (prompt !== undefined) return Math.trunc((prompt * 100) / EFFECTIVE_CONTEXT_WINDOW_PERCENT);
  if (output !== undefined) return DEFAULT_CONTEXT_WINDOW + output;
  return DEFAULT_CONTEXT_WINDOW;
}

function canonicalCatalogIdentifier(value: string): string {
  const providerless = value.split("/").at(-1) ?? value;
  return providerless.toLowerCase().replace(/__(?:dev|max)$/, "");
}

/** 只有可信的内置 config/model 配对才继承 Codex 派生 GPT 的临时窗口上限。 */
function isCodexDerivedGpt(
  cfg: Record<string, unknown>,
  displayConfig: Record<string, unknown>,
  configName: string,
  modelName: string,
): boolean {
  if (cfg["config_source"] === 2 || cfg["config_source"] === 3 || displayConfig["is_custom_model"] === true) {
    return false;
  }
  const configId = canonicalCatalogIdentifier(configName);
  return configId.includes("gpt-5") && configId === canonicalCatalogIdentifier(modelName);
}

/** 解析 get_detail_param 的响应。 */
export function parseCatalog(resp: unknown): CatalogModel[] {
  if (typeof resp !== "object" || resp === null) {
    throw new Error("get_detail_param: response is not an object");
  }
  const list = (resp as Record<string, unknown>)["config_info_list"];
  if (!Array.isArray(list)) {
    throw new Error("get_detail_param: missing config_info_list");
  }

  const out: CatalogModel[] = [];
  for (const rawCfg of list) {
    if (typeof rawCfg !== "object" || rawCfg === null) continue;
    const cfg = rawCfg as Record<string, unknown>;

    if (cfg["usage"] !== "chat_completion"
      || cfg["is_invisible_to_user"] === true
      || cfg["config_switch"] !== true) continue;

    const configName = typeof cfg["config_name"] === "string" ? cfg["config_name"] : "";
    if (configName === "") continue;

    const models = cfg["model_detail_list"];
    if (!Array.isArray(models) || models.length === 0) continue;

    const dc = (typeof cfg["display_config"] === "object" && cfg["display_config"] !== null
      ? cfg["display_config"]
      : {}) as Record<string, unknown>;

    const displayName = typeof dc["display_name"] === "string" ? dc["display_name"] : "";
    const base = displayName !== "" ? displayName : configName;

    const commercial = (typeof dc["commercial_info"] === "object" && dc["commercial_info"] !== null
      ? dc["commercial_info"]
      : {}) as Record<string, unknown>;

    // 计费/限制 flag 全部来自 config 级的 display_config，被该 config 的所有 variant 继承
    const usageLimited = asBoolean(dc["is_internal_usage_limit"]);
    const repoRestricted = asBoolean(dc["is_l4_repo_restricted"]);
    const superModel = asBoolean(dc["can_use_super_model"]);
    const feeLevel = asNumber(dc["fee_model_level"]);
    const manualUsage = asNumber(commercial["manual_usage"]);
    const hotInfo = (typeof dc["hot_info"] === "object" && dc["hot_info"] !== null
      ? dc["hot_info"]
      : {}) as Record<string, unknown>;
    const load = optionalNumber(hotInfo["hot"]);

    for (const rawM of models) {
      if (typeof rawM !== "object" || rawM === null) continue;
      const m = rawM as Record<string, unknown>;
      const modelName = typeof m["model_name"] === "string" ? m["model_name"] : "";
      if (modelName === "") continue;

      const reasoning = parseReasoningEfforts(m["model_extra_config"]);
      const rawContextWindow = contextWindow(m["prompt_max_tokens"], m["max_tokens"]);
      out.push({
        id: variantId(base, modelName),
        config: configName,
        model: modelName,
        contextWindow: isCodexDerivedGpt(cfg, dc, configName, modelName)
          ? Math.min(rawContextWindow, CODEX_DERIVED_CONTEXT_WINDOW_CAP)
          : rawContextWindow,
        usageLimited,
        repoRestricted,
        superModel,
        feeLevel,
        manualUsage,
        ...(load !== undefined ? { load } : {}),
        ...(reasoning ? { reasoning } : {}),
      });
    }
  }

  if (out.length === 0) throw new Error("get_detail_param: empty config_info_list");
  return out;
}

export class CatalogStore {
  private byId = new Map<string, CatalogModel>();
  /** 小写 id → 规范 id，用于大小写不敏感查找（保留首次出现的规范拼写） */
  private folded = new Map<string, string>();
  /** config_name → 该 config 的 `__max` 变体，供 preferMax 用 */
  private maxByCfg = new Map<string, CatalogModel>();
  private ids: string[] = [];

  set(models: CatalogModel[]): void {
    const byId = new Map<string, CatalogModel>();
    const folded = new Map<string, string>();
    const maxByCfg = new Map<string, CatalogModel>();
    const ids: string[] = [];

    for (const m of models) {
      if (!byId.has(m.id)) ids.push(m.id);
      byId.set(m.id, m);
      const lower = m.id.toLowerCase();
      if (!folded.has(lower)) folded.set(lower, m.id);
      if (m.model.endsWith("__max")) maxByCfg.set(m.config, m);
    }

    ids.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    this.byId = byId;
    this.folded = folded;
    this.maxByCfg = maxByCfg;
    this.ids = ids;
  }

  /** 精确匹配优先，失败则大小写不敏感 */
  lookup(id: string): CatalogModel | undefined {
    const exact = this.byId.get(id);
    if (exact) return exact;
    const canonical = this.folded.get(id.toLowerCase());
    return canonical ? this.byId.get(canonical) : undefined;
  }

  /** 若该 config 有 `__max` 变体则返回它，否则返回原 entry */
  preferMax(e: CatalogModel): CatalogModel {
    return this.maxByCfg.get(e.config) ?? e;
  }

  list(): CatalogModel[] {
    return this.ids.map((id) => this.byId.get(id)!).filter(Boolean);
  }

  get size(): number {
    return this.byId.size;
  }

  /** metered 模型 = catalog 里 is_internal_usage_limit 为 true 的 */
  meteredModelNames(): string[] {
    return this.list().filter((m) => m.usageLimited).map((m) => m.model);
  }
}

export interface CatalogRefresherOptions {
  client: UpstreamClient;
  store: CatalogStore;
  logger?: { info(...a: unknown[]): void; warn(...a: unknown[]): void; error(...a: unknown[]): void };
}

export class CatalogRefresher {
  private timer: NodeJS.Timeout | null = null;

  constructor(private readonly opts: CatalogRefresherOptions) {}

  /** 启动时的第一次拉取。失败直接抛，让进程启动失败。 */
  async refreshOnce(): Promise<void> {
    const resp = await this.opts.client.getDetailParam();
    this.opts.store.set(parseCatalog(resp));
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      void this.opts.client
        .getDetailParam()
        .then((resp) => {
          this.opts.store.set(parseCatalog(resp));
        })
        .catch((e: unknown) => {
          // 刷新失败保留上一份好数据，不影响在跑的服务
          this.opts.logger?.warn(`catalog refresh failed, keeping last good copy: ${(e as Error).message}`);
        });
    }, CATALOG_TTL_MS);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
