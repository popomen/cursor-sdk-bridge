/**
 * OpenAI Chat Completions 请求 → 统一中间形态。
 *
 * 中间形态本来就是 Chat Completions 形状，所以这层基本是直通 + 字段筛选。
 */

import type { UnifiedChatRequest, UnifiedMessage, UnifiedTool } from "../types/unified.js";
import { normalizeToolParameters } from "./tool-schema.js";
import { asBool, usesStructuredOutput } from "./upstream-body.js";

export interface ChatParseResult {
  request: UnifiedChatRequest;
  requestedModel: string;
  stream: boolean;
  /** stream_options.include_usage —— 决定是否在流末尾发 usage chunk */
  includeUsage: boolean;
  /** response_format 会通过合成工具实现，响应时需要还原成 JSON 文本。 */
  structuredOutput: boolean;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function openaiChatToUnified(body: Record<string, unknown>, defaultModel: string): ChatParseResult {
  const requestedModel = typeof body["model"] === "string" && body["model"] !== "" ? body["model"] : defaultModel;
  const stream = asBool(body["stream"]);

  let includeUsage = false;
  const so = body["stream_options"];
  if (isPlainObject(so)) includeUsage = so["include_usage"] === true;

  const request: UnifiedChatRequest = {
    model: requestedModel,
    messages: Array.isArray(body["messages"]) ? (body["messages"] as UnifiedMessage[]) : [],
    stream,
  };

  if (Array.isArray(body["tools"])) {
    // 与 Anthropic / Responses 路径一致地清洗 schema —— 同一个上游，同样挑剔
    request.tools = (body["tools"] as UnifiedTool[]).map((t) => {
      if (!isPlainObject(t) || !isPlainObject(t["function"])) return t;
      const fn = t["function"] as Record<string, unknown>;
      return { ...t, function: { ...fn, parameters: normalizeToolParameters(fn["parameters"]) } };
    });
  }
  if (body["tool_choice"] !== undefined) request.tool_choice = body["tool_choice"];
  if (body["response_format"] !== undefined) request.response_format = body["response_format"];

  const numeric = ["max_tokens", "max_completion_tokens", "temperature", "top_p", "seed"] as const;
  for (const k of numeric) {
    const v = body[k];
    if (typeof v === "number" && Number.isFinite(v)) {
      // max_completion_tokens 是 max_tokens 的新名字
      request[k === "max_completion_tokens" ? "max_tokens" : k] = v;
    }
  }

  if (typeof body["parallel_tool_calls"] === "boolean") {
    request.parallel_tool_calls = body["parallel_tool_calls"];
  }
  if (typeof body["reasoning_effort"] === "string") request.reasoning_effort = body["reasoning_effort"];

  return { request, requestedModel, stream, includeUsage, structuredOutput: usesStructuredOutput(request) };
}
