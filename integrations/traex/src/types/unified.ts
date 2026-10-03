/**
 * 统一中间形态。
 *
 * 上游 `llm_raw_chat` 说的是类 OpenAI Chat Completions 的方言，所以中间形态
 * 就以 Chat Completions 为骨架 —— 三个下游协议（Chat / Responses / Anthropic）
 * 都先转成这个形状，再由唯一的 buildUpstreamBody 转成 Trae wire。
 */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = Record<string, unknown>;

/** 上游接受的 content part 类型白名单 */
export const ALLOWED_PART_TYPES = new Set([
  "text",
  "image_url",
  "image",
  "input_audio",
  "audio_url",
  "audio",
]);

export interface UnifiedToolCall {
  id?: string;
  type?: string;
  index?: number;
  function?: { name?: string; arguments?: string };
  /** 上游 wire 用的键名（Q2）。转换过程中两种形态都可能出现。 */
  function_call?: { name?: string; arguments?: string };
  [k: string]: unknown;
}

export interface UnifiedMessage {
  role: string;
  content: unknown;
  tool_calls?: UnifiedToolCall[];
  tool_call_id?: string;
  name?: string;
  [k: string]: unknown;
}

export interface UnifiedTool {
  type?: string;
  function?: {
    name?: string;
    description?: string;
    /** 进上游前会被 JSON.stringify 成字符串（Q1） */
    parameters?: unknown;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export interface UnifiedChatRequest {
  model?: string;
  messages?: UnifiedMessage[];
  stream?: boolean;
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  seed?: number;
  parallel_tool_calls?: boolean;
  tools?: UnifiedTool[];
  /** 进上游前会归一成字符串（auto / none / required / 工具名） */
  tool_choice?: unknown;
  /** Chat structured-output 输入；不原样下发 raw-chat。 */
  response_format?: unknown;
  reasoning?: unknown;
  reasoning_effort?: string;
  /** Responses structured-output 输入；不原样下发 raw-chat。 */
  text?: unknown;
  store?: boolean;
  include?: unknown;
  client_metadata?: unknown;
  [k: string]: unknown;
}

/** 上游 `event: output` 的归一化形态 */
export interface UpstreamOutputEvent {
  /** 正文增量。上游可能用 `response`(旧) 或 `content`(新)，旧字段优先 */
  response: string;
  /** 思维链增量。`reasoning_content`(旧) 或 `reasoning`(新)，旧字段优先 */
  reasoningContent: string;
  toolCalls: UpstreamToolCallDelta[];
}

/** 上游 tool call 增量的归一化形态 */
export interface UpstreamToolCallDelta {
  index: number;
  id: string;
  type: string;
  name: string;
  arguments: string;
}

export interface UpstreamUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  reasoning_tokens: number;
  cache_read_input_tokens: number;
  cache_write_input_tokens: number;
}

/** 排队事件的归一化形态 */
export interface QueueEvent {
  event: string;
  position?: number;
  hasPosition: boolean;
  queueId: string;
  requestUuid: string;
  message: string;
}
