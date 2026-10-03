/**
 * OpenAI Responses 请求 → 统一中间形态。
 *
 * Responses 的工具模型比 Chat 复杂：有 namespace 分组、custom（自由文本）工具、
 * hosted 工具。上游只认扁平的 function 工具，所以这里要展开、改名、清洗 schema，
 * 并记下 meta 以便响应侧还原回客户端认识的形状。
 */

import type { UnifiedChatRequest, UnifiedMessage, UnifiedTool } from "../types/unified.js";
import {
  EMPTY_TOOL_OUTPUT_PLACEHOLDER,
  normalizeToolParameters,
} from "./tool-schema.js";
import { asBool, usesStructuredOutput } from "./upstream-body.js";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export interface ToolMeta {
  /** 客户端原始工具名 */
  name: string;
  namespace: string;
  /** custom 工具：参数是自由文本，包在 {"input": "..."} 信封里 */
  custom: boolean;
  /** 原始 type：function / custom / tool_search */
  kind: string;
}

export type ToolMetaMap = Map<string, ToolMeta>;

export interface ResponsesParseResult {
  request: UnifiedChatRequest;
  requestedModel: string;
  stream: boolean;
  toolMeta: ToolMetaMap;
  empty: boolean;
  /** text.format 会通过合成工具实现，响应时需要还原成 JSON 文本。 */
  structuredOutput: boolean;
}

export class InvalidResponsesInputError extends Error {
  readonly statusCode = 400;

  constructor(message: string) {
    super(message);
    this.name = "InvalidResponsesInputError";
  }
}

function safeToolNamePart(s: string): string {
  const cleaned = s.replace(/[^A-Za-z0-9_-]/g, "_").replace(/^[_-]+|[_-]+$/g, "");
  return cleaned === "" ? "tool" : cleaned;
}

/** custom 工具在上游被表示成一个只有 `input` 字符串参数的 function */
function customToolAsFunction(upstreamName: string, description: string): UnifiedTool {
  const fn: Record<string, unknown> = {
    name: upstreamName,
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: { input: { type: "string", description: "Raw input for the custom tool." } },
      required: ["input"],
    },
  };
  if (description !== "") fn["description"] = description;
  return { type: "function", function: fn };
}

function withNamespaceDescription(description: string, namespace: string): string {
  if (namespace === "") return description;
  const prefix = `Namespace: ${namespace}.`;
  if (description === "") return prefix;
  if (description.includes(prefix)) return description; // 幂等
  return `${prefix}\n\n${description}`;
}

interface ToolPlan {
  tools: UnifiedTool[];
  meta: ToolMetaMap;
  /** (原名, namespace) → 上游名，用于回传时反查 */
  reverse: Map<string, string>;
}

/** 采集所有工具声明：req.tools + input 里的 additional_tools / tool_search_output */
function rawTools(body: Record<string, unknown>): unknown[] {
  const out: unknown[] = [];
  if (Array.isArray(body["tools"])) out.push(...body["tools"]);
  const input = body["input"];
  if (Array.isArray(input)) {
    for (const item of input) {
      if (!isPlainObject(item)) continue;
      const t = str(item["type"]);
      if (t === "additional_tools" || t === "tool_search_output") {
        if (Array.isArray(item["tools"])) out.push(...item["tools"]);
      }
    }
  }
  return out;
}

export function buildToolPlan(body: Record<string, unknown>): ToolPlan {
  const tools: UnifiedTool[] = [];
  const meta: ToolMetaMap = new Map();
  const reverse = new Map<string, string>();
  const used = new Set<string>();
  const seen = new Set<string>();

  const appendTool = (raw: unknown, namespace: string): void => {
    if (!isPlainObject(raw)) return;

    // namespace / toolbox 分组：展开成子工具
    const nested = raw["tools"];
    const rawType = str(raw["type"]);
    if (Array.isArray(nested) && nested.length > 0 &&
        (rawType === "namespace" || rawType === "toolbox" || rawType === "")) {
      const ns = str(raw["name"]) || namespace;
      for (const child of nested) appendTool(child, ns);
      return;
    }

    const type = rawType === "" ? "function" : rawType;

    // hosted 工具必须由 provider 侧执行；暴露成普通 function 会让客户端
    // 误以为本地执行了它
    if (type === "web_search" || type === "web_search_preview" || type === "file_search" ||
        type === "computer_use_preview" || type === "code_interpreter") {
      return;
    }

    const fnObj = isPlainObject(raw["function"]) ? raw["function"] : undefined;
    let name = str(raw["name"]);
    if (name === "" && fnObj) name = str(fnObj["name"]);

    if (type === "tool_search") {
      if (name === "") name = "tool_search";
    }
    if (name === "") return;

    // 去重：同 (namespace, name) 只保留第一个
    const dedupKey = `${namespace}\u0000${name}`;
    if (seen.has(dedupKey)) return;
    seen.add(dedupKey);

    // 上游不支持 namespace 结构，但 namespace 是工具身份的一部分，不能只在
    // 叶子名重名时才保留。响应侧会通过 meta 把全限定名称还原成 namespace + name。
    const baseCandidate = namespace === ""
      ? name
      : `${safeToolNamePart(namespace)}__${safeToolNamePart(name)}`;
    let candidate = baseCandidate;
    for (let i = 2; used.has(candidate); i++) candidate = `${baseCandidate}__${i}`;
    used.add(candidate);

    meta.set(candidate, { name, namespace, custom: type === "custom", kind: type });
    reverse.set(dedupKey, candidate);

    if (type === "custom") {
      const desc = withNamespaceDescription(str(raw["description"]), namespace);
      tools.push(customToolAsFunction(candidate, desc));
      return;
    }

    // function / tool_search / 其他带 schema 的类型 → 扁平 function
    const source = fnObj ?? raw;
    const description = withNamespaceDescription(
      str(source["description"]) || str(raw["description"]),
      namespace,
    );
    const params = source["parameters"];
    const fn: Record<string, unknown> = { name: candidate };
    if (description !== "") fn["description"] = description;
    fn["parameters"] =
      params === undefined || params === null
        ? { type: "object", properties: {}, additionalProperties: false }
        : normalizeToolParameters(params);

    tools.push({ type: "function", function: fn });
  };

  for (const t of rawTools(body)) appendTool(t, "");
  return { tools, meta, reverse };
}

/** custom 工具的参数信封 */
export function customInputToArguments(input: string): string {
  return JSON.stringify({ input });
}

/** 解开一层 {"input": "..."} 信封；解不开就原样返回 */
export function unwrapCustomInput(args: string): string {
  try {
    const obj = JSON.parse(args) as unknown;
    if (isPlainObject(obj)) {
      const keys = Object.keys(obj);
      if (keys.length === 1 && keys[0] === "input" && typeof obj["input"] === "string") {
        return obj["input"];
      }
    }
  } catch {
    /* 不是信封 */
  }
  return args;
}

/**
 * content part → 上游 part
 *
 * `includeEncrypted` 只对 `agent_message` 开启：Codex 多智能体之间用
 * `encrypted_content` 传任务载荷，丢掉它子 agent 就收不到任务内容。
 * 普通 user/assistant 消息里的该 part 仍然丢弃（那是客户端私有状态，不该进上游上下文）。
 */
function contentToParts(content: unknown, includeEncrypted = false): unknown[] {
  if (typeof content === "string") return content === "" ? [] : [{ type: "text", text: content }];
  if (!Array.isArray(content)) return [];

  const out: unknown[] = [];
  for (const raw of content) {
    if (!isPlainObject(raw)) continue;
    const type = str(raw["type"]);

    if (type === "input_text" || type === "output_text" || type === "text") {
      const t = str(raw["text"]);
      if (t !== "") out.push({ type: "text", text: t });
    } else if (type === "encrypted_content") {
      if (includeEncrypted) {
        const t = str(raw["encrypted_content"]);
        if (t !== "") out.push({ type: "text", text: t });
      }
    } else if (type === "input_image" || type === "image" || type === "image_url") {
      const iu = raw["image_url"];
      const detail = str(raw["detail"]);
      if (typeof iu === "string" && iu !== "") {
        out.push({
          type: "image_url",
          image_url: { url: iu, ...(detail !== "" ? { detail } : {}) },
        });
      } else if (isPlainObject(iu)) {
        out.push({
          type: "image_url",
          image_url: { ...iu, ...(detail !== "" ? { detail } : {}) },
        });
      } else if (str(raw["file_id"]) !== "") {
        throw new InvalidResponsesInputError("input_image.file_id is not supported; provide image_url or a data URL");
      }
    } else if (type === "input_audio" || type === "audio" || type === "audio_url") {
      const inputAudio = raw["input_audio"];
      const audioUrl = raw["audio_url"];
      if (isPlainObject(inputAudio)) out.push({ type: "input_audio", input_audio: inputAudio });
      else if (audioUrl !== undefined) out.push({ type: "audio_url", audio_url: audioUrl });
    } else if (type === "input_file" || type === "file") {
      throw new InvalidResponsesInputError(
        "input_file is not supported by the upstream protocol; provide extracted text or supported image content",
      );
    }
  }
  return out;
}

/**
 * 工具调用的 arguments 归一成字符串。
 *
 * `function_call.arguments` 本来就是字符串，但 `tool_search_call.arguments` 是对象。
 * 只按字符串读会把后者丢成空值。
 */
function argumentsToString(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === undefined || v === null) return "";
  return JSON.stringify(v);
}

interface ToolOutputParts {
  toolParts: unknown[];
  mediaParts: unknown[];
}

/** function_call_output 的内容取值顺序；上游 tool 消息只保留文本，媒体单独带出。 */
function outputParts(item: Record<string, unknown>): ToolOutputParts {
  for (const key of ["output", "content", "result", "input"]) {
    const v = item[key];
    if (typeof v === "string" && v !== "") {
      return { toolParts: [{ type: "text", text: v }], mediaParts: [] };
    }
    if (Array.isArray(v)) {
      const parts = contentToParts(v);
      if (parts.length > 0) {
        return {
          toolParts: parts.filter((part) => isPlainObject(part) && part["type"] === "text"),
          mediaParts: parts.filter((part) => !isPlainObject(part) || part["type"] !== "text"),
        };
      }
    }
    if (isPlainObject(v)) {
      return { toolParts: [{ type: "text", text: JSON.stringify(v) }], mediaParts: [] };
    }
  }
  return { toolParts: [], mediaParts: [] };
}

export function responsesToUnified(
  body: Record<string, unknown>,
  defaultModel: string,
): ResponsesParseResult {
  const plan = buildToolPlan(body);
  const messages: UnifiedMessage[] = [];

  const instructions = str(body["instructions"]);
  if (instructions.trim() !== "") {
    messages.push({ role: "system", content: [{ type: "text", text: instructions }] });
  }

  const input = body["input"];
  if (typeof input === "string") {
    if (input !== "") messages.push({ role: "user", content: [{ type: "text", text: input }] });
  } else if (Array.isArray(input)) {
    /**
     * 连续的 function_call 要合并成**一条** assistant 消息。
     *
     * 每个工具调用单独成一条消息会破坏「tool 结果消息必须紧跟声明它的 assistant 消息」
     * 这个邻接约束：两个并行工具会展开成 assistant(a) / assistant(b) / tool(a) / tool(b)，
     * 模型看到的历史里工具结果和调用对不上。Codex 默认开 parallel_tool_calls，
     * 多工具轮是常态。
     */
    const pendingToolCalls: Array<Record<string, unknown>> = [];
    const flushPendingToolCalls = (): void => {
      if (pendingToolCalls.length === 0) return;
      messages.push({
        role: "assistant",
        content: [{ type: "text", text: " " }], // Q3：带 tool_calls 的 assistant content 不能空
        tool_calls: pendingToolCalls.map((tc, i) => ({ ...tc, index: i })),
      });
      pendingToolCalls.length = 0;
    };
    const pendingToolOutputs: Array<{ callId: string; toolParts: unknown[]; mediaParts: unknown[] }> = [];
    const flushPendingToolOutputs = (): void => {
      if (pendingToolOutputs.length === 0) return;
      const mediaGroups: Array<{ callId: string; mediaParts: unknown[] }> = [];
      for (const pending of pendingToolOutputs) {
        const msg: UnifiedMessage = {
          role: "tool",
          content: pending.toolParts.length > 0
            ? pending.toolParts
            : [{ type: "text", text: pending.mediaParts.length > 0
              ? "[media attached below]"
              : EMPTY_TOOL_OUTPUT_PLACEHOLDER }],
        };
        if (pending.callId !== "") msg.tool_call_id = pending.callId;
        messages.push(msg);
        if (pending.mediaParts.length > 0) {
          mediaGroups.push({ callId: pending.callId, mediaParts: pending.mediaParts });
        }
      }
      if (mediaGroups.length > 0) {
        const content: unknown[] = [];
        for (let i = 0; i < mediaGroups.length; i++) {
          const group = mediaGroups[i]!;
          content.push(
            { type: "text", text: `Media from tool result ${group.callId || `#${i + 1}`}:` },
            ...group.mediaParts,
          );
        }
        messages.push({ role: "user", content });
      }
      pendingToolOutputs.length = 0;
    };

    for (const raw of input) {
      if (!isPlainObject(raw)) continue;
      // type 缺失视为 message —— 有些客户端在普通对话轮不带 type
      const type = str(raw["type"]) || "message";

      const isToolCallItem =
        type === "function_call" || type === "custom_tool_call" || type === "tool_search_call";
      const isToolOutputItem =
        type === "function_call_output" || type === "custom_tool_call_output" || type === "tool_search_output";
      if (!isToolCallItem) flushPendingToolCalls();
      if (!isToolOutputItem) flushPendingToolOutputs();

      if (type === "message" || type === "agent_message") {
        // agent_message 才保留 encrypted_content（Codex 多智能体的任务载荷）
        const parts = contentToParts(raw["content"], type === "agent_message");
        if (parts.length === 0) continue; // parts 为空则整条丢弃
        messages.push({ role: str(raw["role"]) || "user", content: parts });
        continue;
      }

      if (type === "input_image" || type === "image" || type === "image_url" ||
          type === "input_audio" || type === "audio" || type === "audio_url" ||
          type === "input_file" || type === "file") {
        const parts = contentToParts([raw]);
        if (parts.length > 0) messages.push({ role: "user", content: parts });
        continue;
      }

      if (isToolCallItem) {
        const name = str(raw["name"]);
        const namespace = str(raw["namespace"]);
        const upstreamName =
          plan.reverse.get(`${namespace}\u0000${name}`) ??
          plan.reverse.get(`\u0000${name}`) ??
          (name !== "" ? name : "tool_search");

        let callId = str(raw["call_id"]);
        if (callId === "" && type !== "tool_search_call") callId = str(raw["id"]);
        if (callId === "") continue; // server 端执行的调用没有 call_id，跳过

        // tool_search_call 的 arguments 是**对象**（`{"query":"..."}`），不是字符串。
        // 按字符串读会拿到空值，查询词在历史回放里就丢了。
        let args = argumentsToString(raw["arguments"]);
        if (type === "custom_tool_call") {
          args = customInputToArguments(str(raw["input"]));
        } else if (plan.meta.get(upstreamName)?.custom) {
          args = customInputToArguments(args);
        }
        if (args === "") args = "{}";

        pendingToolCalls.push({
          id: callId,
          type: "function",
          function: { name: upstreamName, arguments: args },
        });
        continue;
      }

      if (isToolOutputItem) {
        const explicitCallId = str(raw["call_id"]) || str(raw["tool_call_id"]);
        const parts = outputParts(raw);
        // tool_search_output 的结果在 `tools` 字段里，不在 output/content/result/input。
        // 走通用取值会拿到空，模型就看不到上一轮搜到了哪些工具，于是反复重搜或幻觉调用。
        if (parts.toolParts.length === 0 && parts.mediaParts.length === 0 &&
            type === "tool_search_output" && Array.isArray(raw["tools"])) {
          parts.toolParts = [{ type: "text", text: JSON.stringify(raw["tools"]) }];
        }
        if (
          type === "function_call_output" &&
          explicitCallId === "" &&
          str(raw["name"]) !== ""
        ) {
          // Named standalone outputs carry an item id (fco_*), not an upstream call id.
          // Trae cannot pair them with a tool call, so preserve them as user context.
          flushPendingToolOutputs();
          const textParts = parts.toolParts.length > 0
            ? parts.toolParts
            : [{ type: "text", text: parts.mediaParts.length > 0
              ? "[media attached below]"
              : EMPTY_TOOL_OUTPUT_PLACEHOLDER }];
          messages.push({
            role: "user",
            content: [...textParts, ...parts.mediaParts],
          });
          continue;
        }
        const callId = explicitCallId || str(raw["id"]);
        pendingToolOutputs.push({ callId, toolParts: parts.toolParts, mediaParts: parts.mediaParts });
        continue;
      }

      // additional_tools 只贡献工具声明，不产生消息
      // item_reference / reasoning：无状态代理无法解引用，静默丢弃
    }
    flushPendingToolCalls(); // input 以工具调用结尾时别漏掉
    flushPendingToolOutputs();
  }

  const requestedModel = str(body["model"]) || defaultModel;
  // 和官方 Responses API 一致：stream 缺省为 false，返回一个完整 JSON。
  // 曾经在这里默认成 true，结果不传 stream 的标准 SDK 拿到的是 SSE，解析直接失败。
  const stream = asBool(body["stream"]);

  const request: UnifiedChatRequest = { model: requestedModel, messages, stream };
  if (plan.tools.length > 0) request.tools = plan.tools;

  // tool_choice：把工具名换成改名后的上游名
  const tc = body["tool_choice"];
  if (typeof tc === "string") {
    request.tool_choice = tc;
  } else if (isPlainObject(tc)) {
    const fn = isPlainObject(tc["function"]) ? tc["function"] : tc;
    const name = str(fn["name"]);
    if (name !== "") {
      const ns = str(fn["namespace"]);
      const upstream = plan.reverse.get(`${ns}\u0000${name}`) ?? plan.reverse.get(`\u0000${name}`) ?? name;
      request.tool_choice = { type: "function", function: { name: upstream } };
    }
  }

  const maxOut = body["max_output_tokens"];
  if (typeof maxOut === "number") request.max_tokens = maxOut;
  const maxTok = body["max_tokens"];
  if (typeof maxTok === "number") request.max_tokens = maxTok; // 后写胜出

  for (const k of ["temperature", "top_p", "seed"] as const) {
    const v = body[k];
    if (typeof v === "number") request[k] = v;
  }
  if (typeof body["parallel_tool_calls"] === "boolean") request.parallel_tool_calls = body["parallel_tool_calls"];
  if (typeof body["reasoning_effort"] === "string") request.reasoning_effort = body["reasoning_effort"];
  if (body["text"] !== undefined) request.text = body["text"];

  // reasoning.effort 派生出扁平的 reasoning_effort
  if (request.reasoning_effort === undefined && isPlainObject(body["reasoning"])) {
    const effort = str((body["reasoning"] as Record<string, unknown>)["effort"]);
    if (effort !== "") request.reasoning_effort = effort;
  }

  return {
    request,
    requestedModel,
    stream,
    toolMeta: plan.meta,
    empty: messages.length === 0,
    structuredOutput: usesStructuredOutput(request),
  };
}
