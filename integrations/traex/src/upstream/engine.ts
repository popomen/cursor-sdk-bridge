/**
 * 上游流引擎：把 Trae SSE 消费成统一事件流，三个下游协议共用。
 *
 * 关键契约：**上游必须给出终止事件（`done` 或 `finish_reason`）**。
 * EOF 但没收到终止事件 = 流不完整 = 失败，绝不能当成功返回。
 * 协议桥不能替上游撒谎。
 */

import type { QueueEvent, UpstreamOutputEvent, UpstreamUsage } from "../types/unified.js";
import {
  classifyUpstreamErrorEvent,
  isQueueWaitEventName,
  parseOutputEvent,
  parseQueueEvent,
  parseUsage,
  ToolCallAccumulator,
} from "./events.js";
import { readUpstreamSSE } from "./sse.js";

/**
 * 上游流的生命周期回调。
 *
 * 三个协议的 transform 都会在相应时机调用它们，具体做什么由 app 层决定
 * （打排队日志、记用量）。所有回调只观察当前请求，不建立跨请求状态。
 */
export interface StreamHooks {
  /** Any non-error activity, including unknown events, makes admission replay unsafe. */
  onActivity?: () => void;
  /** 观察到排队事件 */
  onQueue?: (q: QueueEvent) => void;
  /** 200 流内的 error 事件 —— 「额度耗尽」的典型形态就长这样 */
  onErrorEvent?: (raw: string) => void;
  /** 当前请求的累计 usage 快照；多帧时后值覆盖前值。 */
  onUsage?: (usage: UpstreamUsage) => void;
  /** 流走完（无论成败），统一携带最终 outcome 清理当前请求观测。 */
  onFinish?: (outcome: UpstreamOutcome) => void;
}

export type EngineEvent =
  /** 排队等待。下游可以透传成 SSE 注释行，标准客户端会忽略 */
  | { type: "queue"; queue: QueueEvent }
  /** 内容增量：正文 / 思维链 / 工具调用 */
  | { type: "delta"; output: UpstreamOutputEvent }
  | { type: "usage"; usage: UpstreamUsage }
  /** 流正常终止 */
  | { type: "terminal"; finishReason: string }
  /** 200 流内的 error 事件（"usage limit used up" 的典型形态） */
  | { type: "errorEvent"; raw: string }
  /** output 事件的 JSON 解析失败 */
  | { type: "protocolError"; raw: string }
  /** SSE 读取错误 / idle timeout */
  | { type: "readError"; error: Error };

/**
 * 终止状态分类。判定顺序即优先级，不能乱：
 * 下游断开 > error 事件（含额度/超窗/队列满） > protocol error > read error / 无终止事件 >
 * length / content_filter > sensitive_content
 */
export type UpstreamOutcome =
  | "completed"
  | "downstream_canceled"
  | "upstream_error_event"
  | "upstream_error_timeout"
  | "upstream_error_rate_limit"
  | "upstream_error_quota"
  | "upstream_error_invalid_request"
  | "upstream_protocol_error"
  | "upstream_idle_timeout"
  | "upstream_read_error"
  | "upstream_eof_without_terminal"
  | "upstream_finish_length"
  | "upstream_content_filter"
  | "upstream_sensitive_content"
  | "upstream_unknown_finish";

export interface StreamState {
  sawTerminal: boolean;
  finishReason: string;
  errorEvent: string;
  protocolError: string;
  readError: Error | null;
  downstreamCanceled: boolean;
  /** 只累计 assistant 正文；空 finish_reason 的安全判定不把 reasoning 当最终输出。 */
  accumulatedText: string;
  /** 当前上游流唯一的工具调用累积器，负责生成并固定 synthetic id。 */
  toolCalls: ToolCallAccumulator;
  /**
   * 用独立的布尔标记而不是「字符串非空」来判定是否出错。
   * 上游完全可能发一个内容为空的 error 事件或畸形 payload，
   * 用空串当哨兵会把它判成「没出错」，最后归因到别的原因上。
   */
  sawErrorEvent: boolean;
  sawProtocolError: boolean;
}

export function newStreamState(): StreamState {
  return {
    sawTerminal: false,
    finishReason: "",
    errorEvent: "",
    protocolError: "",
    readError: null,
    downstreamCanceled: false,
    accumulatedText: "",
    toolCalls: new ToolCallAccumulator(),
    sawErrorEvent: false,
    sawProtocolError: false,
  };
}

const LENGTH_REASONS = new Set(["length", "max_tokens", "max_output_tokens"]);
const CONTENT_FILTER_REASONS = new Set(["content_filter", "content_filtered"]);
/**
 * sensitive_content 判定为 **failed 而不是 incomplete** —— 语义不同：
 * length 是不可重试的（输出确实到顶了），sensitive 是可重试的（换个说法可能就过了）。
 */
const SENSITIVE_REASONS = new Set(["sensitive_content", "sensitive"]);

/** 已知的正常结束原因。白名单之外的一律不当作「正常完成」。 */
const NORMAL_FINISH_REASONS = new Set(["stop", "end_turn", "tool_calls", "tool_use", "complete", "completed"]);

/** 空 finish_reason 只有在已收到可证明完整的 payload 时才能 fail open。 */
function emptyFinishPayloadIsComplete(s: StreamState): boolean {
  if (s.accumulatedText !== "") return true;
  if (s.toolCalls.size === 0) return false;
  return s.toolCalls.sorted().every((tool) => {
    if (tool.name === "") return false;
    if (tool.type === "custom") return tool.arguments !== "";
    try {
      JSON.parse(tool.arguments);
      return true;
    } catch {
      return false;
    }
  });
}

export function classifyOutcome(s: StreamState): UpstreamOutcome {
  if (s.downstreamCanceled) return "downstream_canceled";

  if (s.sawErrorEvent) {
    const classified = classifyUpstreamErrorEvent(s.errorEvent);
    if (classified.terminal) {
      if (classified.category === "quota") return "upstream_error_quota";
      if (classified.category === "invalid_request") return "upstream_error_invalid_request";
      if (classified.category === "rate_limit") return "upstream_error_rate_limit";
    }
    const lower = s.errorEvent.toLowerCase();
    if (lower.includes("timeout") || lower.includes("timed out")) return "upstream_error_timeout";
    if (lower.includes("rate limit") || lower.includes("rate_limit")) return "upstream_error_rate_limit";
    return "upstream_error_event";
  }

  if (s.sawProtocolError) return "upstream_protocol_error";

  if (s.readError) {
    return s.readError.message.includes("idle timeout") ? "upstream_idle_timeout" : "upstream_read_error";
  }

  if (!s.sawTerminal) return "upstream_eof_without_terminal";

  const fr = s.finishReason.trim().toLowerCase();
  if (fr === "") return emptyFinishPayloadIsComplete(s) ? "completed" : "upstream_unknown_finish";
  if (LENGTH_REASONS.has(fr)) return "upstream_finish_length";
  if (CONTENT_FILTER_REASONS.has(fr)) return "upstream_content_filter";
  if (SENSITIVE_REASONS.has(fr)) return "upstream_sensitive_content";

  // 上游新增语义（比如 tool_budget_exhausted、safety_stop）时，不能默认当成功。
  // 协议桥不替上游撒谎——不认识就说不认识。
  if (!NORMAL_FINISH_REASONS.has(fr)) return "upstream_unknown_finish";

  return "completed";
}

export function isFailureOutcome(o: UpstreamOutcome): boolean {
  return o !== "completed" && o !== "upstream_finish_length" && o !== "upstream_content_filter";
}

const ERROR_EVENT_NAMES = new Set(["error", "gateway-error", "gateway_error"]);

/**
 * 消费上游 SSE，产出统一事件。同时把终止信息写进 `state` 供 classifyOutcome 用。
 */
export async function* runUpstreamStream(
  body: ReadableStream<Uint8Array>,
  state: StreamState,
  idleMs: number,
  signal?: AbortSignal,
  hooks?: StreamHooks,
): AsyncGenerator<EngineEvent, void, void> {
  try {
    yield* runUpstreamStreamInner(body, state, idleMs, signal, hooks);
  } catch (error) {
    if (!state.readError) state.readError = error instanceof Error ? error : new Error(String(error));
    throw error;
  } finally {
    if (signal?.aborted) state.downstreamCanceled = true;
    hooks?.onFinish?.(classifyOutcome(state));
  }
}

async function* runUpstreamStreamInner(
  body: ReadableStream<Uint8Array>,
  state: StreamState,
  idleMs: number,
  signal: AbortSignal | undefined,
  hooks: StreamHooks | undefined,
): AsyncGenerator<EngineEvent, void, void> {
  for await (const item of readUpstreamSSE(body, idleMs, signal)) {
    if (item.error) {
      state.readError = item.error;
      yield { type: "readError", error: item.error };
      return;
    }
    const ev = item.event;
    if (!ev) continue;

    const name = (ev.event ?? "").trim().toLowerCase();
    if (!ERROR_EVENT_NAMES.has(name) && (name !== "" || ev.data.trim() !== "")) {
      hooks?.onActivity?.();
    }

    // 排队事件先于内容处理 —— 它可能出现在流的任何位置
    if (isQueueWaitEventName(name)) {
      let data: unknown;
      try {
        data = JSON.parse(ev.data);
      } catch {
        data = {};
      }
      const queue = parseQueueEvent(name, data);
      hooks?.onQueue?.(queue);
      yield { type: "queue", queue };
      continue;
    }

    if (name === "output" || name === "") {
      // 空 data 帧是 WHATWG 合法的，nginx 之类的中间层常拿它做保活。
      // 当成协议错误会把整条正常的流杀掉。
      if (ev.data.trim() === "") continue;

      let data: unknown;
      try {
        data = JSON.parse(ev.data);
      } catch {
        state.protocolError = ev.data.slice(0, 2048);
        state.sawProtocolError = true;
        yield { type: "protocolError", raw: state.protocolError };
        return;
      }
      const parsed = parseOutputEvent(data);
      state.accumulatedText += parsed.response;
      const toolCalls = parsed.toolCalls.map((call) => {
        const accumulated = state.toolCalls.apply(call);
        return call.id === accumulated.id ? call : { ...call, id: accumulated.id };
      });
      yield { type: "delta", output: { ...parsed, toolCalls } };
      continue;
    }

    if (name === "token_usage" || name === "usage") {
      let data: unknown;
      try {
        data = JSON.parse(ev.data);
      } catch {
        continue; // usage 解析失败不致命，忽略
      }
      const usage = parseUsage(data);
      hooks?.onUsage?.(usage);
      yield { type: "usage", usage };
      continue;
    }

    if (name === "done" || name === "finish_reason") {
      let finishReason = "";
      try {
        const data = JSON.parse(ev.data) as { finish_reason?: unknown };
        if (typeof data.finish_reason === "string") finishReason = data.finish_reason;
      } catch {
        /* 空 payload 的 done 也算正常终止 */
      }
      state.sawTerminal = true;
      state.finishReason = finishReason;
      yield { type: "terminal", finishReason };
      return;
    }

    if (ERROR_EVENT_NAMES.has(name)) {
      state.errorEvent = ev.data;
      state.sawErrorEvent = true;
      // 200 流内的 error 事件是「额度耗尽」最常见的形态，必须计入用量统计，
      // 否则 /v1/usage 的 live_limited 永远不会亮
      hooks?.onErrorEvent?.(ev.data);
      yield { type: "errorEvent", raw: ev.data };
      // 已知终止码之后往往没有 done；继续等会空转到 idle timeout。
      // 未知码仍继续，兼容 error 后再发 terminal 的旧形态。
      if (classifyUpstreamErrorEvent(ev.data).terminal) return;
      continue;
    }

    // 未知事件名：忽略
  }
}
