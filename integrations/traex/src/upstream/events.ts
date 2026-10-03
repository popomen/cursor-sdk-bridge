/**
 * 上游 SSE 事件的解析与归一化。
 *
 * Trae 会按模型选不同 adapter，同一个 endpoint 上新旧两套字段名都可能出现，
 * 而且**同一个工具调用的首帧和续帧可能用不同形状**。这里把所有已知变体归一。
 */

import { randomUUID } from "node:crypto";
import type { QueueEvent, UpstreamOutputEvent, UpstreamToolCallDelta, UpstreamUsage } from "../types/unified.js";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function num(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v.trim())) return Number(v.trim());
  return 0;
}

/**
 * 解析 `event: output`。
 *
 * 新旧字段双兼容，**旧字段优先**（两者同时存在时用旧的）：
 *   正文：  `response`(旧) / `content`(新)
 *   思维链：`reasoning_content`(旧) / `reasoning`(新)
 */
export function parseOutputEvent(data: unknown): UpstreamOutputEvent {
  const o = isPlainObject(data) ? data : {};

  const oldResp = str(o["response"]);
  const newResp = str(o["content"]);
  const oldReasoning = str(o["reasoning_content"]);
  const newReasoning = str(o["reasoning"]);

  const rawCalls = o["tool_calls"];
  const toolCalls: UpstreamToolCallDelta[] = Array.isArray(rawCalls)
    ? rawCalls.map(parseToolCallDelta).filter((t): t is UpstreamToolCallDelta => t !== null)
    : [];

  return {
    response: oldResp !== "" ? oldResp : newResp,
    reasoningContent: oldReasoning !== "" ? oldReasoning : newReasoning,
    toolCalls,
  };
}

/**
 * 归一化一个 tool call 增量。接受四种 wire 形状：
 *
 *   A  provider-native（首帧最常见）
 *      {"index":1,"id":"call_1","type":"function","function_call":{"name":"f","arguments":"{\"a\":"}}
 *   B  OpenAI 风格（**续帧最常见**，只带 arguments）
 *      {"index":1,"function":{"arguments":"1}"}}
 *   C  顶层扁平
 *      {"index":1,"tool_call_id":"call_2","name":"exec","arguments":"{}"}
 *   D  `input` 作为 arguments 的别名
 *      {"index":1,"name":"apply_patch","input":"*** Begin Patch"}
 *
 * 覆盖优先级：`function` > `function_call` > 顶层，且**只有非空值才覆盖**。
 * 若只认 `function_call` 而忽略 `function`，会出现「首个参数片段留下、后续片段全丢」
 * 的静默 bug —— 拼出的 JSON 残缺，且没有任何错误提示。
 */
export function parseToolCallDelta(raw: unknown): UpstreamToolCallDelta | null {
  if (!isPlainObject(raw)) return null;

  const index = num(raw["index"]);

  let id = str(raw["id"]);
  if (id === "") id = str(raw["tool_call_id"]);

  const type = str(raw["type"]);
  let name = str(raw["name"]);

  let args = str(raw["arguments"]);
  if (args === "") args = str(raw["input"]); // D

  // 先 function_call 覆盖
  const fc = raw["function_call"];
  if (isPlainObject(fc)) {
    const n = str(fc["name"]);
    const a = str(fc["arguments"]);
    if (n !== "") name = n;
    if (a !== "") args = a;
  }

  // 再 function 覆盖（优先级更高）
  const fn = raw["function"];
  if (isPlainObject(fn)) {
    const n = str(fn["name"]);
    const a = str(fn["arguments"]);
    if (n !== "") name = n;
    if (a !== "") args = a;
  }

  return { index, id, type, name, arguments: args };
}

/** 解析 `event: token_usage`（别名 `event: usage`） */
export function parseUsage(data: unknown): UpstreamUsage {
  const o = isPlainObject(data) ? data : {};
  const prompt = num(o["prompt_tokens"]);
  const completion = num(o["completion_tokens"]);
  let total = num(o["total_tokens"]);
  if (total === 0) total = prompt + completion;

  let cacheWrite = num(o["cache_write_input_tokens"]);
  if (cacheWrite === 0) cacheWrite = num(o["cache_write_tokens"]); // 别名
  if (cacheWrite === 0) cacheWrite = num(o["cache_creation_input_tokens"]); // TraeX/Anthropic 别名
  let cacheRead = num(o["cache_read_input_tokens"]);
  if (cacheRead === 0) cacheRead = num(o["cached_input_tokens"]); // TraeX 别名

  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
    reasoning_tokens: num(o["reasoning_tokens"]),
    cache_read_input_tokens: cacheRead,
    cache_write_input_tokens: cacheWrite,
  };
}

/**
 * 排队等待事件的名字判定。
 * 注意 `queue_end` **不算**排队等待事件。
 */
export function isQueueWaitEventName(name: string | undefined): boolean {
  if (!name) return false;
  const n = name.trim().toLowerCase();
  if (n === "queue_begin" || n === "request_wait_in_queue") return true;
  return n.includes("queue") && (n.includes("wait") || n.includes("begin"));
}

/**
 * 从 message 文本里抠出排队位置（position 字段缺失时的兜底）。
 *
 * 只接受 "position" 之后**紧跟**数字的形态，中间最多允许一个连接词/标点。
 * 这样两种真实文案都能命中：
 *   "You're in the queue at position 908."            → 908
 *   "Your queue position is 981. Please wait a while." → 981
 * 而 "position unknown, retry in 30s" 不会误吃后面的 retry delay。
 */
export function queuePositionFromMessage(message: string): number | undefined {
  const m = /position\s*(?:is\s+|are\s+|[:=#]\s*)?(\d+)/i.exec(message);
  return m ? Number(m[1]) : undefined;
}

/** 解析排队事件 payload */
export function parseQueueEvent(eventName: string, data: unknown): QueueEvent {
  const o = isPlainObject(data) ? data : {};
  const message = str(o["message"]);

  // position 字段名兼容三种拼写
  let position: number | undefined;
  for (const key of ["position", "queue_position", "queuePosition"]) {
    const v = o[key];
    if (v !== undefined && v !== null) {
      const n = num(v);
      if (n !== 0 || v === 0 || v === "0") {
        position = n;
        break;
      }
    }
  }
  // 字段缺失时从 message 文本里抠
  if (position === undefined && message !== "") {
    position = queuePositionFromMessage(message);
  }

  return {
    event: eventName,
    ...(position !== undefined ? { position } : {}),
    hasPosition: position !== undefined,
    queueId: str(o["queue_id"]),
    requestUuid: str(o["request_uuid"]),
    message,
  };
}

export type UpstreamErrorCategory = "quota" | "invalid_request" | "rate_limit" | "auth" | "";

/** SSE `event: error` 里已识别的数值业务码。未知码不当成终止。 */
export const UPSTREAM_ERROR_CODE_INVALID_PARAM = 4001;
export const UPSTREAM_ERROR_CODE_QUOTA = 4008;
export const UPSTREAM_ERROR_CODE_MODEL_UNAVAILABLE = 4037;
export const UPSTREAM_ERROR_CODE_QUEUE_FULL = 4050;

export interface ClassifiedUpstreamError {
  category: UpstreamErrorCategory;
  /**
   * 已知终止码 / 超窗：立刻结束上游流，不再等 `done`。
   * 未知码保持 false，兼容「error 之后仍发 terminal」的旧形态。
   */
  terminal: boolean;
  code?: number;
  message: string;
}

function extractErrorCode(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isInteger(value)) return value;
  if (typeof value === "string" && /^-?\d+$/.test(value.trim())) return Number(value.trim());
  return undefined;
}

function fallbackMessageForCode(code: number): string {
  switch (code) {
    case UPSTREAM_ERROR_CODE_INVALID_PARAM:
      return "request parameter is invalid";
    case UPSTREAM_ERROR_CODE_QUOTA:
      return "本周模型额度已用完或当前模型额度不足，请切换模型后重试。";
    case UPSTREAM_ERROR_CODE_MODEL_UNAVAILABLE:
      return "当前仓库密级较高，所选模型不可用，请切换到支持 L4 仓库的模型。";
    case UPSTREAM_ERROR_CODE_QUEUE_FULL:
      return "the request queue is full";
    default:
      return `upstream error code=${code}`;
  }
}

/** 解析 SSE error 事件 data：`{"code": 4050, "message": "..."}`。非 JSON 则整段当 message。 */
export function parseUpstreamErrorEvent(payload: string): { code?: number; message: string } {
  const trimmed = payload.trim();
  if (trimmed.startsWith("{")) {
    try {
      const obj = JSON.parse(trimmed) as unknown;
      if (isPlainObject(obj)) {
        return { code: extractErrorCode(obj["code"]), message: str(obj["message"]) };
      }
    } catch {
      /* 不是 JSON，整段当 message */
    }
  }
  return { message: trimmed };
}

export function isContextWindowError(text: string): boolean {
  const p = text.toLowerCase();
  return (
    p.includes("context_length_exceeded") ||
    p.includes("prompt is too long") ||
    p.includes("context window exceeded")
  );
}

/**
 * 给下游看的 error 文案：有结构化 message 就用它；空 message 用可读兜底，并保留数值码。
 * 非 JSON 原文原样返回，避免把纯文本再包一层。
 */
export function formatUpstreamErrorEvent(payload: string): string {
  const parsed = parseUpstreamErrorEvent(payload);
  const classified = classifyUpstreamErrorEvent(payload);
  const unstructured = parsed.code === undefined && parsed.message === payload.trim();
  if (unstructured) return payload;
  const msg = classified.message;
  if (parsed.code !== undefined && !msg.includes(String(parsed.code))) {
    return `${msg} (code=${parsed.code})`;
  }
  return msg;
}

/**
 * SSE error 事件分类。**数值码优先于文本启发式**；超窗文案无论 code 是什么都升为
 * invalid_request 且立即终止。未知码不终止，交给文本启发式（可能仍是空分类）。
 */
export function classifyUpstreamErrorEvent(payload: string): ClassifiedUpstreamError {
  const parsed = parseUpstreamErrorEvent(payload);
  const haystack = `${payload}\n${parsed.message}`;
  const message = parsed.message !== ""
    ? parsed.message
    : parsed.code !== undefined
      ? fallbackMessageForCode(parsed.code)
      : payload;

  if (isContextWindowError(haystack)) {
    return { category: "invalid_request", terminal: true, code: parsed.code, message };
  }

  if (parsed.code === UPSTREAM_ERROR_CODE_INVALID_PARAM) {
    return { category: "invalid_request", terminal: true, code: parsed.code, message };
  }
  if (parsed.code === UPSTREAM_ERROR_CODE_QUOTA) {
    return { category: "quota", terminal: true, code: parsed.code, message };
  }
  if (parsed.code === UPSTREAM_ERROR_CODE_MODEL_UNAVAILABLE) {
    return { category: "invalid_request", terminal: true, code: parsed.code, message };
  }
  if (parsed.code === UPSTREAM_ERROR_CODE_QUEUE_FULL) {
    return { category: "rate_limit", terminal: true, code: parsed.code, message };
  }

  return {
    category: classifyUpstreamErrorFromHints(0, payload),
    terminal: false,
    code: parsed.code,
    message,
  };
}

/**
 * 上游错误分类。**按顺序匹配，先命中先返回** —— 顺序是两个真实踩坑的结果：
 *
 *  - 数值业务码（4001/4008/4037/4050）和超窗文案优先于一切文本启发式。
 *  - `quota` 排在 `rate_limit` 前：额度耗尽的消息可能带 429 状态，要标成 quota
 *    而不是泛化的 rate_limit。
 *  - `invalid_request` 排在 `rate_limit` 前：聚合错误体会给**每次尝试**都
 *    追加 "model QPM/TPM rate limited"，即使真实原因是我们自己发的 400。不这样排
 *    会把自己的 bad request 误标成限流，并错误地把模型标记为受限。
 */
export function classifyUpstreamError(status: number, payload: string): UpstreamErrorCategory {
  const event = classifyUpstreamErrorEvent(payload);
  if (event.terminal) return event.category;
  return classifyUpstreamErrorFromHints(status, payload);
}

function classifyUpstreamErrorFromHints(status: number, payload: string): UpstreamErrorCategory {
  const p = payload.toLowerCase();

  const quotaHints = [
    "quota", "usage limit", "usage_limit", "used up", "exhausted",
    "insufficient_quota", "out of credit", "credit", "balance", "billing",
  ];
  if (quotaHints.some((h) => p.includes(h))) return "quota";

  const invalidHints = [
    "invalid_request_error", "missing_required_parameter", "missing_required",
    "unsupported_parameter", "unknown_parameter", "invalid_value", "bad request",
  ];
  if (status === 400 || invalidHints.some((h) => p.includes(h))) return "invalid_request";

  const rateHints = ["rate limit", "rate_limit", "too many requests", "throttl"];
  if (status === 429 || rateHints.some((h) => p.includes(h))) return "rate_limit";

  const authHints = [
    "unauthorized", "forbidden", "invalid token", "token expired", "expired",
    "authentication", "permission denied", "access denied",
  ];
  if (status === 401 || status === 403 || authHints.some((h) => p.includes(h))) return "auth";

  return "";
}

/**
 * 跨 chunk 累积 tool call 参数。
 * 按上游 `index` 分桶 —— 同 index 的 arguments 片段按到达顺序拼接。
 */
export class ToolCallAccumulator {
  private readonly byIndex = new Map<number, { id: string; type: string; name: string; args: string[] }>();
  private readonly order: number[] = [];

  constructor(
    private readonly idFactory: () => string = () => `call_${randomUUID().replaceAll("-", "")}`,
  ) {}

  apply(delta: UpstreamToolCallDelta): { index: number; id: string; isNew: boolean } {
    let st = this.byIndex.get(delta.index);
    const isNew = st === undefined;
    if (!st) {
      // 同一个调用的 id 一旦确定就不能再变：流式协议可能已经把首帧发给客户端。
      // 上游首帧没有 id 时在这里统一生成，避免六条下游 transform 各自随机兜底。
      st = {
        id: delta.id !== "" ? delta.id : this.idFactory(),
        type: "",
        name: "",
        args: [],
      };
      this.byIndex.set(delta.index, st);
      this.order.push(delta.index);
    }
    // type/name 的续帧非空值可以补齐；id 则坚持 first effective id wins。
    if (delta.type !== "") st.type = delta.type;
    if (delta.name !== "") st.name = delta.name;
    if (delta.arguments !== "") st.args.push(delta.arguments);
    return { index: delta.index, id: st.id, isNew };
  }

  get(index: number): { id: string; type: string; name: string; arguments: string } | undefined {
    const st = this.byIndex.get(index);
    if (!st) return undefined;
    return { id: st.id, type: st.type, name: st.name, arguments: st.args.join("") };
  }

  /** 按上游 index 升序返回全部累积结果 */
  sorted(): Array<{ index: number; id: string; type: string; name: string; arguments: string }> {
    return [...this.byIndex.keys()]
      .sort((a, b) => a - b)
      .map((i) => ({ index: i, ...this.get(i)! }));
  }

  get size(): number {
    return this.byIndex.size;
  }
}
