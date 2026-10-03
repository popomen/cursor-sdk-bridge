/**
 * Anthropic Messages 请求 → 统一中间形态。
 *
 * Anthropic 把 tool_result 放在 user 消息的 content blocks 里，而上游要的是
 * 独立的 `role:"tool"` 消息 —— 所以一条 Anthropic 消息可能展开成多条上游消息。
 */

import type { UnifiedChatRequest, UnifiedMessage, UnifiedTool } from "../types/unified.js";
import { normalizeToolParameters } from "./tool-schema.js";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** `system` 支持 string 与 block 数组两种形态 */
export function systemToText(system: unknown): string {
  if (typeof system === "string") return system;
  if (Array.isArray(system)) {
    let sb = "";
    for (const b of system) {
      if (!isPlainObject(b)) continue;
      if (b["type"] !== "text") continue;
      sb += str(b["text"]);
    }
    return sb;
  }
  return "";
}

function textPart(block: Record<string, unknown>): Record<string, unknown> | null {
  const text = str(block["text"]);
  if (text === "") return null;
  const part: Record<string, unknown> = { type: "text", text };
  if (isPlainObject(block["cache_control"])) part["cache_control"] = block["cache_control"];
  return part;
}

/**
 * tool_result.content 归一。text 拼成纯文本；image 块转成上游 image_url part 单独带出。
 *
 * 上游的 role:"tool" 消息只吃文本，但 Claude Code 的 Read 工具读图片时，
 * 图片恰好在 tool_result 里回传 —— 丢弃它等于废掉整条「工具读图」路径，
 * 且空 output 会让部分上游直接 4027（tool call failed: No tool output）。
 * 图片由调用方追加成紧随其后的 user 消息（上游对 user 消息里的图片是支持的）。
 */
function toolResultParts(content: unknown): { text: string; images: unknown[] } {
  if (typeof content === "string") return { text: content, images: [] };
  if (Array.isArray(content)) {
    let sb = "";
    const images: unknown[] = [];
    for (const b of content) {
      if (!isPlainObject(b)) continue;
      if (b["type"] === "text") sb += str(b["text"]);
      else if (b["type"] === "image") {
        const p = imageBlockToPart(b);
        if (p) images.push(p);
      }
    }
    return { text: sb, images };
  }
  if (content === null || content === undefined) return { text: "", images: [] };
  return { text: JSON.stringify(content), images: [] };
}

/** Anthropic image block → 上游的 image_url part */
function imageBlockToPart(block: Record<string, unknown>): unknown | null {
  const source = block["source"];
  if (!isPlainObject(source)) return null;

  const sourceType = str(source["type"]);
  if (sourceType === "base64") {
    const mediaType = str(source["media_type"]) || "image/png";
    const data = str(source["data"]);
    if (data === "") return null;
    return { type: "image_url", image_url: { url: `data:${mediaType};base64,${data}` } };
  }
  if (sourceType === "url") {
    const url = str(source["url"]);
    if (url === "") return null;
    return { type: "image_url", image_url: { url } };
  }
  return null;
}

export interface AnthropicToChatResult {
  request: UnifiedChatRequest;
  /** 客户端请求的 model 原文（响应里要回显这个，不是上游 model_name） */
  requestedModel: string;
  stream: boolean;
  /** 客户端传的 stop_sequences，用于响应里回填 stop_sequence */
  stopSequences: string[];
}

/**
 * Anthropic Messages 请求 → 统一形态。
 */
export function anthropicToUnified(
  body: Record<string, unknown>,
  defaultModel: string,
  anthropicBeta?: unknown,
): AnthropicToChatResult {
  const messages: UnifiedMessage[] = [];

  // system 先于所有消息
  const rawSystem = body["system"];
  if (Array.isArray(rawSystem)) {
    const systemParts: unknown[] = [];
    for (const block of rawSystem) {
      if (!isPlainObject(block) || block["type"] !== "text") continue;
      const part = textPart(block);
      if (part) systemParts.push(part);
    }
    if (systemParts.length > 0) messages.push({ role: "system", content: systemParts });
  } else {
    const systemText = systemToText(rawSystem);
    if (systemText.trim() !== "") {
      messages.push({ role: "system", content: [{ type: "text", text: systemText }] });
    }
  }

  const rawMessages = Array.isArray(body["messages"]) ? body["messages"] : [];
  for (const rawMsg of rawMessages) {
    if (!isPlainObject(rawMsg)) continue;
    const role = str(rawMsg["role"]) || "user";
    const content = rawMsg["content"];

    if (typeof content === "string") {
      messages.push({ role, content: [{ type: "text", text: content }] });
      continue;
    }

    if (!Array.isArray(content)) {
      messages.push({ role, content: [{ type: "text", text: "" }] });
      continue;
    }

    const parts: unknown[] = [];
    const toolUses: Array<{ id: string; name: string; input: unknown }> = [];
    const toolResults: Array<{ id: string; text: string; images: unknown[] }> = [];

    for (const rawBlock of content) {
      if (!isPlainObject(rawBlock)) {
        parts.push({ type: "text", text: String(rawBlock) });
        continue;
      }
      const type = str(rawBlock["type"]);

      if (type === "text") {
        const part = textPart(rawBlock);
        if (part) parts.push(part);
      } else if (type === "image") {
        const p = imageBlockToPart(rawBlock);
        if (p) parts.push(p);
      } else if (type === "tool_use") {
        // 只有 assistant 轮的 tool_use 才是模型真实发起的调用。允许 user 轮的
        // tool_use 转成 tool_calls，等于让调用方伪造一段模型的工具调用历史，
        // 而且上游 schema 里也不存在 `role:"user" + tool_calls` 这种形状。
        if (role === "assistant") {
          toolUses.push({
            id: str(rawBlock["id"]),
            name: str(rawBlock["name"]),
            input: rawBlock["input"],
          });
        }
      } else if (type === "tool_result") {
        const tr = toolResultParts(rawBlock["content"]);
        toolResults.push({
          id: str(rawBlock["tool_use_id"]),
          text: tr.text,
          images: tr.images,
        });
      } else if (type === "thinking" || type === "redacted_thinking") {
        // 上游 llm_raw_chat 不接受回传的思维链，丢弃。
        // 不做 JSON 序列化塞进 text —— 那会污染上下文并浪费 token。
      } else {
        parts.push({ type: "text", text: JSON.stringify(rawBlock) });
      }
    }

    // tool_result 拆成独立的 role:"tool" 消息，顺序在前。
    //
    // 关键：这一串 tool 消息之间绝不能插入别的角色。上游会把「紧跟 assistant 的
    // 一串连续 role:"tool" 消息」重组成 Anthropic 的一条 tool_result 消息，与
    // assistant 的 tool_use 一一相邻。一旦在中间插一条 user 图片消息，就打断了
    // 这个连续段——并行工具调用时后面的 tool_use 就找不到「紧邻的 tool_result」，
    // 上游 Bedrock 直接 400（tool_use ids ... without tool_result blocks
    // immediately after），且这段消息进入会话历史后每次重试都会复现同样的错误。
    //
    // 带图片的 tool_result：tool 消息里留占位文本，图片全部攒到最后，用一条 user
    // 消息统一补出（上游支持 user 消息里的图），既保证连续段不被打断，又看得到图。
    if (toolResults.length > 0) {
      const pendingImageGroups: Array<{ id: string; images: unknown[] }> = [];
      for (const tr of toolResults) {
        const text = tr.text !== "" ? tr.text : tr.images.length > 0 ? "[image attached below]" : "";
        const tm: UnifiedMessage = {
          role: "tool",
          content: [{ type: "text", text }],
        };
        if (tr.id !== "") tm.tool_call_id = tr.id;
        messages.push(tm);
        if (tr.images.length > 0) pendingImageGroups.push({ id: tr.id, images: tr.images });
      }
      if (pendingImageGroups.length > 0) {
        const imageParts: unknown[] = [];
        for (let i = 0; i < pendingImageGroups.length; i++) {
          const group = pendingImageGroups[i]!;
          const label = group.id !== "" ? group.id : `#${i + 1}`;
          imageParts.push({ type: "text", text: `Image(s) from tool result ${label}:` }, ...group.images);
        }
        messages.push({
          role: "user",
          content: imageParts,
        });
      }
      if (parts.length > 0) messages.push({ role, content: parts });
      continue;
    }

    // tool_use → tool_calls
    if (toolUses.length > 0) {
      const msg: UnifiedMessage = {
        role,
        content: parts.length > 0 ? parts : [{ type: "text", text: " " }], // Q3
        tool_calls: toolUses.map((tu, i) => ({
          index: i,
          id: tu.id !== "" ? tu.id : `toolu_${randomHex(16)}`,
          type: "function",
          function: {
            name: tu.name,
            arguments: tu.input === undefined || tu.input === null ? "{}" : JSON.stringify(tu.input),
          },
        })),
      };
      messages.push(msg);
      continue;
    }

    messages.push({ role, content: parts.length > 0 ? parts : [{ type: "text", text: "" }] });
  }

  // tools
  let tools: UnifiedTool[] | undefined;
  const rawTools = body["tools"];
  if (Array.isArray(rawTools)) {
    const converted: UnifiedTool[] = [];
    for (const rawTool of rawTools) {
      if (!isPlainObject(rawTool)) continue;
      const name = str(rawTool["name"]);
      if (name === "") continue;
      const fn: Record<string, unknown> = { name };
      const desc = str(rawTool["description"]);
      if (desc !== "") fn["description"] = desc;
      // 必须清洗：Claude Code 挂的 MCP server 常带 $ref / format / $schema，
      // 上游见到会 400。之前只有 Responses 路径清洗，这条路径漏了。
      fn["parameters"] = normalizeToolParameters(rawTool["input_schema"]);
      converted.push({ type: "function", function: fn });
    }
    if (converted.length > 0) tools = converted;
  }

  const requestedModel = str(body["model"]) || defaultModel;
  // Claude Code 2.1.226+ 不再把 `[1m]` 留在 body.model，而是把基础模型名
  // 和 `anthropic-beta: context-1m-*` 分开发送。统一层用 __max 这个既有安全
  // 后缀表达路由意图，响应仍回显客户端真正发送的 requestedModel。
  const routingModel = hasContext1mBeta(anthropicBeta) && !hasMaxVariantSuffix(requestedModel)
    ? `${requestedModel}__max`
    : requestedModel;
  const stream = body["stream"] === true || body["stream"] === "true" || body["stream"] === "1";

  const stopSequences = Array.isArray(body["stop_sequences"])
    ? body["stop_sequences"].filter((s): s is string => typeof s === "string")
    : [];

  const request: UnifiedChatRequest = {
    model: routingModel,
    messages,
    stream,
  };
  if (tools) request.tools = tools;

  const tc = mapToolChoice(body["tool_choice"]);
  if (tc !== undefined) request.tool_choice = tc;

  if (typeof body["max_tokens"] === "number") request.max_tokens = body["max_tokens"];
  if (typeof body["temperature"] === "number") request.temperature = body["temperature"];
  if (typeof body["top_p"] === "number") request.top_p = body["top_p"];

  // Anthropic extended thinking → 上游 reasoning effort
  const thinking = body["thinking"];
  if (isPlainObject(thinking)) {
    const thinkingType = str(thinking["type"]);
    if (thinkingType === "adaptive") {
      // 新版 Claude Code 用 output_config.effort 搭配 adaptive thinking。
      const outputConfig = body["output_config"];
      const effort = isPlainObject(outputConfig) ? str(outputConfig["effort"]) : "";
      if (effort !== "") {
        request.reasoning = { effort };
        request.reasoning_effort = effort;
      }
    } else if (thinkingType === "enabled") {
      const budget = thinking["budget_tokens"];
      const effort = budgetToEffort(typeof budget === "number" ? budget : 0);
      request.reasoning = { effort };
      request.reasoning_effort = effort;
    }
  }

  return { request, requestedModel, stream, stopSequences };
}

function hasContext1mBeta(value: unknown): boolean {
  const raw = Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string").join(",")
    : str(value);
  return raw.split(",").some((part) => {
    const token = part.trim().toLowerCase();
    return token === "context-1m" || token.startsWith("context-1m-");
  });
}

function hasMaxVariantSuffix(model: string): boolean {
  const lower = model.toLowerCase();
  return lower.endsWith("[1m]") || lower.endsWith("__max") || lower.endsWith("-max");
}

/** Anthropic 的 thinking budget → 上游的 effort 等级 */
function budgetToEffort(budget: number): string {
  if (budget <= 0) return "medium";
  if (budget <= 4096) return "low";
  if (budget <= 16384) return "medium";
  if (budget <= 32768) return "high";
  return "xhigh";
}

function mapToolChoice(tc: unknown): unknown {
  if (typeof tc === "string") return tc;
  if (!isPlainObject(tc)) return undefined;
  const type = str(tc["type"]);
  if (type === "auto") return "auto";
  if (type === "any") return "required";
  if (type === "none") return "none";
  if (type === "tool") {
    const name = str(tc["name"]);
    if (name === "") return undefined;
    return { type: "function", function: { name } };
  }
  return undefined;
}

export function randomHex(n: number): string {
  const bytes = new Uint8Array(Math.ceil(n / 2));
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, n);
}

/** Chat finish_reason → Anthropic stop_reason */
export function mapStopReason(finishReason: string, hasTools: boolean): string {
  if (hasTools) return "tool_use";
  switch (finishReason.trim().toLowerCase()) {
    case "stop": return "end_turn";
    case "length":
    case "max_tokens":
    case "max_output_tokens": return "max_tokens";
    case "tool_calls":
    case "tool_use": return "tool_use";
    case "content_filter":
    case "content_filtered": return "refusal";
    default: return "end_turn";
  }
}
