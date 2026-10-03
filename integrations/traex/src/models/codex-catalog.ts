/**
 * Codex 客户端 `/models` 目录。
 *
 * Codex 会在请求里携带 `client_version`，并要求 `{"models":[...]}` 以及完整的
 * ModelInfo metadata。模板快照来自 codex-cli 0.147.0 自带目录；实时模型名、
 * 上下文窗口和推理档位仍由 Traex 上游目录决定。
 */

import codexTemplate from "./codex-template.json";
import type { CatalogModel } from "../upstream/catalog.js";

type CodexModel = Record<string, unknown>;

export interface CodexModelsResponse {
  models: CodexModel[];
}

const EFFORT_DESCRIPTIONS: Readonly<Record<string, string>> = {
  none: "No additional reasoning",
  minimal: "Minimal reasoning for the fastest response",
  low: "Fast responses with lighter reasoning",
  medium: "Balances speed and reasoning depth for everyday tasks",
  high: "Greater reasoning depth for complex problems",
  xhigh: "Extra high reasoning depth for complex problems",
  max: "Maximum reasoning depth for the hardest problems",
  ultra: "Maximum reasoning with automatic task delegation",
};

function normalizeEffort(value: string | undefined): string | null {
  if (!value) return null;
  const normalized = value.trim().toLowerCase();
  return Object.hasOwn(EFFORT_DESCRIPTIONS, normalized) ? normalized : null;
}

function reasoningMetadata(model: CatalogModel): {
  defaultLevel: string | null;
  levels: Array<{ effort: string; description: string }>;
} {
  const seen = new Set<string>();
  const levels: Array<{ effort: string; description: string }> = [];
  for (const raw of model.reasoning?.supportedEfforts ?? []) {
    const effort = normalizeEffort(raw);
    if (!effort || seen.has(effort)) continue;
    seen.add(effort);
    levels.push({ effort, description: EFFORT_DESCRIPTIONS[effort]! });
  }
  return {
    // 默认档位和可选档位是两个独立的上游字段；即使 options 为空也要保留默认值。
    defaultLevel: normalizeEffort(model.reasoning?.defaultEffort),
    levels,
  };
}

function codexModel(model: CatalogModel, priority: number): CodexModel {
  const entry = structuredClone(codexTemplate) as unknown as CodexModel;
  const reasoning = reasoningMetadata(model);

  Object.assign(entry, {
    slug: model.id,
    display_name: model.id,
    description: `${model.id} via traex-bridge.`,
    visibility: "list",
    supported_in_api: true,
    priority,
    default_reasoning_level: reasoning.defaultLevel,
    supported_reasoning_levels: reasoning.levels,

    // 这些能力属于 OpenAI 托管后端，TraexBridge 当前只提供 HTTP Responses。
    prefer_websockets: false,
    use_responses_lite: false,
    multi_agent_version: null,
    tool_mode: "direct",
    supports_search_tool: false,
    supports_reasoning_summaries: model.reasoning !== undefined,
    supports_image_detail_original: false,
    web_search_tool_type: "text",
    service_tiers: [],
    additional_speed_tiers: [],
    default_service_tier: null,
    auto_review_model_override: null,
    support_verbosity: false,
    default_verbosity: null,
    supports_parallel_tool_calls: true,
  });

  if (model.contextWindow > 0) {
    entry["context_window"] = model.contextWindow;
    entry["max_context_window"] = model.contextWindow;
  }
  return entry;
}

export function buildCodexModelsResponse(models: readonly CatalogModel[]): CodexModelsResponse {
  return {
    models: models.map((model, index) => codexModel(model, index + 1)),
  };
}
