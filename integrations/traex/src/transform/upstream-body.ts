/**
 * 统一中间形态 → Trae `llm_raw_chat` wire format。
 *
 * 这是唯一的上游请求构造点。下面每一条都是上游的物理约束，违反会静默失败或 400：
 *
 *  Q1  tools[].function.parameters 必须是 **JSON 字符串**，不是对象
 *  Q2  assistant 的 tool call 用 `function_call` 键，不是 OpenAI 的 `function`
 *  Q3  带 tool_calls 的 assistant，content 不能为空 → 填单个空格 " "
 *  Q4  tool_choice 直接发字符串；structured output 改写成合成函数工具
 *  Q5  user_input 必填 = **最后一条** user 消息的文本（不是全部拼接）
 *  Q6  max_tokens 原值下发，缺失时默认 32768
 *  Q7  content 永远归一化成数组
 *  Q8  tool 消息的 output 不能为空 → 填单个空格（空则 4027 tool call failed）
 */

import { randomUUID } from "node:crypto";
import {
  ALLOWED_PART_TYPES,
  type UnifiedChatRequest,
  type UnifiedMessage,
  type UnifiedTool,
  type UnifiedToolCall,
} from "../types/unified.js";
import { normalizeToolParameters } from "./tool-schema.js";

export const DEFAULT_MAX_TOKENS = 32768;
export const DEFAULT_ACCESS_TYPE = 4;
export const STRUCTURED_OUTPUT_TOOL_NAME = "structured_output";

/** 环境覆盖必须是 u8；非法值回退默认值，避免构造出无效 wire 数字。 */
export function accessTypeFromEnv(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env["TRAEX_BRIDGE_ACCESS_TYPE"];
  if (raw === undefined || raw.trim() === "") return DEFAULT_ACCESS_TYPE;
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) return DEFAULT_ACCESS_TYPE;
  const value = Number(trimmed);
  return Number.isInteger(value) && value >= 0 && value <= 255 ? value : DEFAULT_ACCESS_TYPE;
}

/** 只有 true / "true" / "1" 算真（对齐 Go 的 asBool，`stream: 1` 数字不算真） */
export function asBool(v: unknown): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "string") return v === "true" || v === "1";
  return false;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * content 归一化成 part 数组（Q7）。
 *
 * 白名单外的 part type 会被整个 JSON 序列化后塞进一个 text part —— 宁可让模型
 * 看到 JSON 文本，也不能把上游不认识的结构直接发过去。
 */
export function normalizeContent(content: unknown): unknown[] {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (content === null || content === undefined) return [{ type: "text", text: "" }];

  if (Array.isArray(content)) {
    const out: unknown[] = [];
    for (const part of content) {
      if (isPlainObject(part)) {
        const t = part["type"];
        if (typeof t === "string" && ALLOWED_PART_TYPES.has(t)) {
          out.push(part); // 原样保留（含 image_url.detail、input_audio.format 等所有字段）
        } else {
          out.push({ type: "text", text: JSON.stringify(part) });
        }
      } else {
        out.push({ type: "text", text: String(part) });
      }
    }
    return out;
  }

  return [{ type: "text", text: String(content) }];
}

/**
 * 判断 part 数组是否"空"。
 * 纯图片 / 纯音频消息不算空 —— 它们没有 text 但有实际内容。
 */
export function contentPartsEmpty(parts: unknown[]): boolean {
  if (parts.length === 0) return true;
  for (const p of parts) {
    if (!isPlainObject(p)) continue;
    const text = p["text"];
    if (typeof text === "string" && text !== "") return false;
    if ("image_url" in p || "image" in p || "audio_url" in p || "input_audio" in p) return false;
  }
  return true;
}

/**
 * Q2：把 OpenAI 的 `function` 键改名成上游的 `function_call`。
 * 其余键（id / type / index / 未知键）原样保留。
 */
export function normalizeToolCalls(toolCalls: UnifiedToolCall[]): UnifiedToolCall[] {
  return toolCalls.map((tc) => {
    if (!isPlainObject(tc)) return tc;
    const out: UnifiedToolCall = { ...tc };
    if (out.function !== undefined && out.function_call === undefined) {
      out.function_call = out.function;
      delete out.function;
    }
    return out;
  });
}

/** 合并连续 assistant，避免上游收到不满足轮次约束的 assistant/assistant。 */
function appendNormalizedMessage(out: UnifiedMessage[], msg: UnifiedMessage): void {
  const previous = out[out.length - 1];
  if (!previous || previous.role !== "assistant" || msg.role !== "assistant") {
    out.push(msg);
    return;
  }

  const combinedContent = [
    ...(Array.isArray(previous.content) ? previous.content : []),
    ...(Array.isArray(msg.content) ? msg.content : []),
  ];
  const combinedCalls = [
    ...(Array.isArray(previous.tool_calls) ? previous.tool_calls : []),
    ...(Array.isArray(msg.tool_calls) ? msg.tool_calls : []),
  ];
  const hasRealContent = !contentPartsEmpty(combinedContent.filter((part) => {
    return !(isPlainObject(part) && part["type"] === "text" && part["text"] === " ");
  }));
  previous.content = hasRealContent
    ? combinedContent.filter((part) => !(isPlainObject(part) && part["type"] === "text" && part["text"] === " "))
    : [{ type: "text", text: combinedCalls.length > 0 ? " " : "" }];
  if (combinedCalls.length > 0) {
    previous.tool_calls = combinedCalls.map((call, index) => ({ ...call, index }));
  }
  if (!previous.name && msg.name) previous.name = msg.name;
}

/** 规范化 messages：role 兜底、content 转数组、Q2、Q3、连续 assistant 合并 */
export function normalizeMessages(messages: UnifiedMessage[] | undefined): UnifiedMessage[] {
  if (!Array.isArray(messages)) return [];
  const out: UnifiedMessage[] = [];
  const pendingToolMedia: Array<{ id: string; parts: unknown[] }> = [];
  const flushToolMedia = (): void => {
    if (pendingToolMedia.length === 0) return;
    const content: unknown[] = [];
    for (let i = 0; i < pendingToolMedia.length; i++) {
      const group = pendingToolMedia[i]!;
      content.push(
        { type: "text", text: `Media from tool result ${group.id || `#${i + 1}`}:` },
        ...group.parts,
      );
    }
    appendNormalizedMessage(out, { role: "user", content });
    pendingToolMedia.length = 0;
  };

  for (const raw of messages) {
    if (!isPlainObject(raw)) continue;
    const m = raw as UnifiedMessage;

    let role = typeof m.role === "string" && m.role !== "" ? m.role : "user";
    // OpenAI 在 o1 之后用 `developer` 取代 `system`，Codex 发过来的第一条就是它。
    // 上游只认 system/assistant/user/tool —— 有的模型会直接 400，有的容忍，
    // 所以必须在这里统一收敛，不能指望上游。
    if (role === "developer") role = "system";
    if (role !== "tool") flushToolMedia();
    let content = normalizeContent(m.content);

    const toolCalls = Array.isArray(m.tool_calls) ? normalizeToolCalls(m.tool_calls) : undefined;

    // Q3：上游拒绝「有 tool_calls 但 content 为空」的 assistant 消息
    if (role === "assistant" && toolCalls && toolCalls.length > 0 && contentPartsEmpty(content)) {
      content = [{ type: "text", text: " " }];
    }

    // Q8：上游拒绝空 output 的 tool 消息（4027 "No tool output found"）
    if (role === "tool" && contentPartsEmpty(content)) {
      content = [{ type: "text", text: " " }];
    }

    const msg: UnifiedMessage = { role, content };
    if (toolCalls && toolCalls.length > 0) msg.tool_calls = toolCalls;
    if (typeof m.tool_call_id === "string" && m.tool_call_id !== "") msg.tool_call_id = m.tool_call_id;
    if (typeof m.name === "string" && m.name !== "") msg.name = m.name;

    if (role === "tool") {
      const textParts = content.filter((part) => isPlainObject(part) && part["type"] === "text");
      const mediaParts = content.filter((part) => !isPlainObject(part) || part["type"] !== "text");
      if (mediaParts.length > 0) {
        msg.content = contentPartsEmpty(textParts) ? [{ type: "text", text: "[media attached below]" }] : textParts;
        pendingToolMedia.push({ id: msg.tool_call_id ?? "", parts: mediaParts });
      }
    }

    appendNormalizedMessage(out, msg);
  }

  flushToolMedia();

  return out;
}

/**
 * Q5：user_input = 最后一条 role:"user" 消息里所有非空 text part 用 `\n` 连接。
 * image / audio part 不贡献内容。找不到 user 消息则返回空串。
 */
export function lastUserText(messages: UnifiedMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]!;
    if (m.role !== "user") continue;
    if (!Array.isArray(m.content)) return "";
    const texts: string[] = [];
    for (const p of m.content) {
      if (!isPlainObject(p)) continue;
      if (p["type"] !== "text") continue;
      const t = p["text"];
      if (typeof t === "string" && t !== "") texts.push(t);
    }
    return texts.join("\n");
  }
  return "";
}

/**
 * Q1：`parameters` 必须是 JSON 编码后的字符串。
 * 缺失时补 `{"type":"object","properties":{}}` 再字符串化；已经是字符串的不重复编码。
 */
export function stringifyToolParams(tools: UnifiedTool[]): UnifiedTool[] {
  return tools.map((t) => {
    if (!isPlainObject(t)) return t;
    const fn = t["function"];
    if (!isPlainObject(fn)) return t; // 没有 function 键的条目原样放行

    const params = fn["parameters"];
    const effective = normalizeToolParameters(
      params === undefined || params === null ? { type: "object", properties: {} } : params,
    );
    const cleanFunction: Record<string, unknown> = { parameters: JSON.stringify(effective) };
    if (typeof fn["name"] === "string" && fn["name"] !== "") cleanFunction["name"] = fn["name"];
    if (typeof fn["description"] === "string" && fn["description"] !== "") {
      cleanFunction["description"] = fn["description"];
    }
    return { type: "function", function: cleanFunction };
  });
}

/**
 * Q4：raw-chat 的 `tool_choice` 是字符串。标准模式原样下发；具名对象压成工具名。
 */
export function adaptToolChoice(toolChoice: unknown): string | undefined {
  if (typeof toolChoice === "string" && toolChoice !== "") return toolChoice;
  if (!isPlainObject(toolChoice)) return undefined;
  const fn = toolChoice["function"];
  if (isPlainObject(fn) && typeof fn["name"] === "string" && fn["name"] !== "") {
    return fn["name"];
  }
  return typeof toolChoice["name"] === "string" && toolChoice["name"] !== ""
    ? toolChoice["name"]
    : undefined;
}

/** 从 Chat response_format 或 Responses text.format 提取 output schema。 */
export function structuredOutputSchema(req: UnifiedChatRequest): unknown | undefined {
  const responseFormat = req.response_format;
  if (isPlainObject(responseFormat)) {
    const type = responseFormat["type"];
    if (type === "json_object") return { type: "object" };
    if (type === "json_schema") {
      const jsonSchema = responseFormat["json_schema"];
      if (isPlainObject(jsonSchema) && jsonSchema["schema"] !== undefined) return jsonSchema["schema"];
    }
  }

  const text = req.text;
  if (!isPlainObject(text)) return undefined;
  const format = text["format"];
  if (!isPlainObject(format) || format["type"] !== "json_schema") return undefined;
  return format["schema"];
}

export function usesStructuredOutput(req: UnifiedChatRequest): boolean {
  return structuredOutputSchema(req) !== undefined;
}

export interface BuildUpstreamBodyOptions {
  config: string;
  model: string;
}

/**
 * 构造 `llm_raw_chat` 请求体。
 *
 * 透传白名单之外的字段一律静默丢弃（temperature / top_p / seed / reasoning /
 * text / store / include / client_metadata / response_format / metadata / stream_options …）。
 */
export function buildUpstreamBody(
  req: UnifiedChatRequest,
  opts: BuildUpstreamBodyOptions,
): Record<string, unknown> {
  const messages = normalizeMessages(req.messages);
  const realTools = Array.isArray(req.tools) ? req.tools : [];
  const schema = structuredOutputSchema(req);
  const allTools = [...realTools];
  if (schema !== undefined) {
    allTools.push({
      type: "function",
      function: {
        name: STRUCTURED_OUTPUT_TOOL_NAME,
        description: "Return the structured output.",
        parameters: schema,
      },
    });
  }
  const tools = allTools.length > 0 ? stringifyToolParams(allTools) : undefined;

  const body: Record<string, unknown> = {
    config_name: opts.config,
    model_name: opts.model,
    // bridge 不从客户端身份派生或复用上游身份。每个 HTTP 生成请求都是独立事务。
    session_id: randomUUID(),
    conversation_id: randomUUID(),
    messages,
    user_input: lastUserText(messages), // Q5
    is_preset: true,
    access_type: accessTypeFromEnv(),
  };

  // Q6：显式值保持调用方语义，缺失使用 raw-chat 默认值。
  const mt = req.max_tokens;
  if (typeof mt === "number" && Number.isFinite(mt)) {
    body["max_tokens"] = mt;
  } else {
    body["max_tokens"] = DEFAULT_MAX_TOKENS;
  }

  if (tools && tools.length > 0) {
    body["tools"] = tools;
  }

  // 合成工具不能单独触发并行调用；真实 tools 延续既有默认 true 的行为。
  if (realTools.length > 0) {
    body["parallel_tool_calls"] = typeof req.parallel_tool_calls === "boolean"
      ? req.parallel_tool_calls
      : true;
  }

  // structured output 必须最终调用合成工具；存在真实工具时先要求任意工具调用。
  const toolChoice = schema !== undefined
    ? (realTools.length > 0 ? "required" : STRUCTURED_OUTPUT_TOOL_NAME)
    : adaptToolChoice(req.tool_choice);
  if (toolChoice !== undefined) body["tool_choice"] = toolChoice;

  // raw-chat 只接受扁平 effort；ultra 是客户端档位，wire 归一为 max。
  if (typeof req.reasoning_effort === "string" && req.reasoning_effort !== "") {
    body["reasoning_effort"] = req.reasoning_effort.toLowerCase() === "ultra"
      ? "max"
      : req.reasoning_effort;
  }

  return body;
}
