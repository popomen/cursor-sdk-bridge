/**
 * 模型解析：客户端传来的 model 名 → 上游 (config_name, model_name)。
 *
 * 规则链：精确 catalog id → family alias → 配置的 default。
 *
 * 一条关键的不对称：**精确 id 不做 max 偏好，alias 和 default 才做**。
 * 发 `openrouter-3o` 就固定拿到 `openrouter-3o__dev`（这是钉住小 context 变体的
 * 逃生阀）；发 `claude-3-opus-xxx` 会拿到 `openrouter-3o__max`。
 *
 * 偏好 max 的理由：Claude Code 之类客户端整个 session 用固定 model 名，中途切
 * 变体会作废 provider 侧的 context cache，所以一开始就路由到最大窗口。
 */

import type { CatalogModel, CatalogStore } from "../upstream/catalog.js";

/**
 * family alias 表。**顺序敏感**：按子串匹配，第一个命中即返回。
 * `opus` / `sonnet` / `haiku` 必须排在 `claude` 前面，
 * 否则 `claude-3-opus` 会被泛匹配成 sonnet 档。
 */
const ALIASES: ReadonlyArray<readonly [string, string]> = [
  ["opus", "openrouter-3o"],
  ["sonnet", "openrouter-2o"],
  ["haiku", "openrouter-1o"],
  ["gemini", "Gemini-3.1-Pro-Preview"],
  ["deepseek", "DeepSeek-V4-Pro"],
  ["minimax", "MiniMax-M2.7"],
  ["qwen", "Qwen3.6-Plus"],
  ["kimi", "Kimi-K2.6"],
  ["glm", "GLM-5"],
  ["doubao", "Doubao-Seed-Code"],
  ["astra", "GPT-6-Astra"],
  ["gpt-6", "GPT-6-Astra"],
  ["gpt6", "GPT-6-Astra"],
  ["gpt", "GPT-5.5"],
  ["claude", "openrouter-2o"],
];

export function aliasFor(input: string): string {
  const lower = input.toLowerCase();
  for (const [sub, canonical] of ALIASES) {
    if (lower.includes(sub)) return canonical;
  }
  return "";
}

export interface ResolvedModel {
  config: string;
  model: string;
  /** 命中的 catalog 条目（走 fallback 分支时为 undefined） */
  entry?: CatalogModel;
}

/**
 * 精确查找 + 拼写容错。
 *  1. 精确（大小写不敏感）
 *  2. `__max` / `-max` 后缀 → `<base>[1m]`
 *  3. `a__b` → `a-b`
 */
export function lookupModelId(store: CatalogStore, id: string): CatalogModel | undefined {
  if (id === "") return undefined;

  const direct = store.lookup(id);
  if (direct) return direct;

  for (const suffix of ["__max", "-max"]) {
    if (id.length > suffix.length && id.toLowerCase().endsWith(suffix)) {
      const base = id.slice(0, id.length - suffix.length);
      const hit = store.lookup(`${base}[1m]`);
      if (hit) return hit;
      // 客户端可以独立声明 1M beta，即使当前模型没有大上下文变体。
      // 此时保持在显式请求的 base，而不是掉进 family/default 路由到别的模型。
      const baseHit = store.lookup(base);
      if (baseHit) return baseHit;
    }
  }

  if (id.includes("__")) {
    const hit = store.lookup(id.split("__").join("-"));
    if (hit) return hit;
  }

  return undefined;
}

export function resolveModel(store: CatalogStore, input: string, defaultModel: string): ResolvedModel {
  // 1. 精确 catalog id —— 显式指定永远原样生效，不升级到 max 变体
  const exact = lookupModelId(store, input);
  if (exact) return { config: exact.config, model: exact.model, entry: exact };

  // 2. family alias → 该家族的 base entry，再偏好 max 变体
  const canonical = aliasFor(input);
  if (canonical !== "") {
    const hit = store.lookup(canonical);
    if (hit) {
      const preferred = store.preferMax(hit);
      return { config: preferred.config, model: preferred.model, entry: preferred };
    }
  }

  // 3. 配置的 default，同样偏好 max 变体
  const def = lookupModelId(store, defaultModel);
  if (def) {
    const preferred = store.preferMax(def);
    return { config: preferred.config, model: preferred.model, entry: preferred };
  }

  // 4. 连 default 都不是 catalog id → 原样当作 config/model 发上游
  return { config: defaultModel, model: defaultModel };
}
