/**
 * 上游统一事件流 → Anthropic Messages SSE。
 *
 * 协议不变量（Claude Code 会因为违反这些而报错或挂起）：
 *  1. 每个事件同时写 `event:` 行和 JSON 内的 `type` 字段 —— 两者都有客户端在读。
 *  2. content block **不能交错**：任何时刻最多一个块打开，index 全局单调递增、永不复用。
 *  3. thinking 块在 `content_block_stop` 之前必须已经发过 `signature_delta`。
 *  4. text_delta 只能发给 text 块，input_json_delta 只能发给 tool_use 块，
 *     thinking_delta/signature_delta 只能发给 thinking 块。
 *  5. 流结束时所有块必须已封口。
 *  6. 绝不在上游没给终止事件时伪造成功终态。
 */

import type { UpstreamUsage } from "../types/unified.js";
import { formatUpstreamErrorEvent } from "../upstream/events.js";
import { classifyOutcome, isFailureOutcome, newStreamState, runUpstreamStream } from "../upstream/engine.js";
import type { StreamHooks } from "../upstream/engine.js";
import { mapStopReason, randomHex } from "./anthropic-request.js";
import { validateToolBatch } from "./tool-schema.js";

const encoder = new TextEncoder();

function sseFrame(event: string, data: unknown): Uint8Array {
  return encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

/**
 * 合成一个可逆的 thinking 签名。
 *
 * Anthropic 要求 thinking 块封口前有 signature_delta，但上游只给纯文本 reasoning，
 * 没有签名。这里编码成自有格式，客户端回传时我们能认出来（当前实现会丢弃回传的
 * thinking，所以签名只用于满足协议形状）。
 */
const SIGNATURE_PREFIX = "traex-bridge-reasoning-v1:";
export function encodeReasoningSignature(content: string): string {
  return SIGNATURE_PREFIX + Buffer.from(JSON.stringify({ reasoning: content }), "utf8").toString("base64url");
}

type BlockType = "text" | "thinking" | "tool";

export interface AnthropicStreamOptions {
  upstreamBody: ReadableStream<Uint8Array>;
  requestedModel: string;
  stopSequences: string[];
  idleMs: number;
  signal?: AbortSignal;
  /** 排队事件透传成 SSE 注释行 */
  emitQueueComments?: boolean;
  onOutcome?: (outcome: string) => void;
  /** 当前上游流生命周期回调（排队日志 / 用量统计） */
  hooks?: StreamHooks;
}

/** 排队期间向下游发标准 ping 事件的最小间隔 */
const PING_INTERVAL_MS = 10_000;

/**
 * 产出 Anthropic SSE 字节流。
 */
export function anthropicSseStream(opts: AnthropicStreamOptions): ReadableStream<Uint8Array> {
  const state = newStreamState();

  return new ReadableStream<Uint8Array>({
    async start(controller) {
      const messageId = `msg_${randomHex(24)}`;
      let closed = false;

      const push = (chunk: Uint8Array): boolean => {
        if (closed) return false;
        try {
          controller.enqueue(chunk);
          return true;
        } catch {
          closed = true;
          return false;
        }
      };
      const emit = (event: string, data: Record<string, unknown>): boolean =>
        push(sseFrame(event, { type: event, ...data }));

      // ---- 块状态 ----
      let nextIndex = 0;
      let currentIndex = -1;
      let currentType: BlockType | null = null;
      let signatureSent = false;
      let reasoningText = "";

      /** 关闭当前打开的块。thinking 块补发 signature_delta（不变量 3）。 */
      const closeBlock = (): void => {
        if (currentIndex < 0) return;
        if (currentType === "thinking" && !signatureSent) {
          emit("content_block_delta", {
            index: currentIndex,
            delta: { type: "signature_delta", signature: encodeReasoningSignature(reasoningText) },
          });
          signatureSent = true;
        }
        emit("content_block_stop", { index: currentIndex });
        currentIndex = -1;
        currentType = null;
        reasoningText = "";
        signatureSent = false;
      };

      const openBlock = (type: BlockType, contentBlock: Record<string, unknown>): number => {
        closeBlock(); // 不变量 2：开新块前必须先关旧块
        const index = nextIndex++;
        currentIndex = index;
        currentType = type;
        emit("content_block_start", { index, content_block: contentBlock });
        return index;
      };

      // ---- 工具状态 ----
      // 只累积，收尾时统一开块。这样 content block 天然不会交错，
      // 也不会出现「参数被正文打断后丢失」的情况。
      const tools = state.toolCalls;
      /** 真正发到线上的 tool_use 块数 —— stop_reason 必须以它为准 */
      let emittedToolBlocks = 0;

      let usage: UpstreamUsage | null = null;
      let stopReason = "end_turn";
      let stopSequence: string | null = null;
      let lastPingAt = 0;

      // ---- message_start ----
      emit("message_start", {
        message: {
          id: messageId,
          type: "message",
          role: "assistant",
          model: opts.requestedModel,
          content: [],
          stop_reason: null,
          stop_sequence: null,
          usage: { input_tokens: 0, output_tokens: 0 },
        },
      });

      try {
        for await (const ev of runUpstreamStream(opts.upstreamBody, state, opts.idleMs, opts.signal, opts.hooks)) {
          if (opts.signal?.aborted) {
            state.downstreamCanceled = true;
            break;
          }

          if (ev.type === "queue") {
            if (opts.emitQueueComments !== false) {
              // SSE 注释行，标准客户端会忽略；作用是长队列时的 keepalive
              push(encoder.encode(`: trae-queue ${JSON.stringify(ev.queue)}\n\n`));
            }
            // 注释行救不了所有客户端：Claude Code 的流看门狗只认 SSE 事件，
            // 注释被解析器吞掉后 300s 无事件即掐流（"no chunks received"）。
            // 官方 Anthropic API 本来就周期性发 ping，所以这里发标准 ping 事件。
            const now = Date.now();
            if (now - lastPingAt >= PING_INTERVAL_MS) {
              lastPingAt = now;
              emit("ping", {});
            }
            continue;
          }

          if (ev.type === "usage") {
            usage = ev.usage;
            continue;
          }

          if (ev.type === "errorEvent" || ev.type === "protocolError" || ev.type === "readError") {
            break; // 终止判定交给 classifyOutcome
          }

          if (ev.type === "terminal") {
            break;
          }

          if (ev.type !== "delta") continue;
          const out = ev.output;

          // 处理顺序固定：reasoning → text → tools
          if (out.reasoningContent !== "") {
            if (currentType !== "thinking") {
              openBlock("thinking", { type: "thinking", thinking: "" });
            }
            reasoningText += out.reasoningContent;
            emit("content_block_delta", {
              index: currentIndex,
              delta: { type: "thinking_delta", thinking: out.reasoningContent },
            });
          }

          if (out.response !== "") {
            if (currentType !== "text") {
              openBlock("text", { type: "text", text: "" });
            }
            emit("content_block_delta", {
              index: currentIndex,
              delta: { type: "text_delta", text: out.response },
            });
          }

          // tool 由统一 engine 累积，收尾时一次性输出。这样 content block 不会交错，
          // 同时 synthetic id 与同一请求内的其他下游事件保持一致。
        }
      } catch (e) {
        state.readError = e as Error;
      }

      const outcome = classifyOutcome(state);
      opts.onOutcome?.(outcome);

      // 下游已断开：静默退出，不写任何终止事件
      if (outcome === "downstream_canceled" || opts.signal?.aborted) {
        try { controller.close(); } catch { /* already closed */ }
        return;
      }

      if (isFailureOutcome(outcome)) {
        closeBlock();
        // 流已开始，无法再用 HTTP 错误码 —— 把失败编码成协议内的 error 事件。
        // 失败响应绝不能先发任何可执行 tool_use。
        emit("error", {
          error: {
            type: anthropicErrorTypeFor(outcome),
            message: failureMessage(outcome, state.errorEvent),
          },
        });
        try { controller.close(); } catch { /* already closed */ }
        return;
      }

      // ---- 统一输出 tool 块 ----
      //
      // 上游被 length / content_filter 截断时参数会停在半路。把残缺 JSON 当成合法
      // 工具调用发出去，客户端会真的去执行它。所以这里逐个校验：
      //   - 截断类终态 → 降级成文本说明，绝不发出可执行的 tool_use
      //   - 正常终态却参数非法 → 是上游协议问题，整体判失败
      const candidates = tools.sorted().map((tool) => ({
        id: tool.id,
        name: tool.name,
        type: tool.type,
        arguments: tool.arguments,
      }));
      const truncatedTerminal = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
      const batch = validateToolBatch(candidates, truncatedTerminal);
      if (batch.fatal) {
        closeBlock();
        emit("error", {
          error: { type: "api_error", message: `tool call ${batch.fatal.name}: ${batch.fatal.reason}` },
        });
        try { controller.close(); } catch { /* already closed */ }
        return;
      }
      if (batch.incompleteText !== "") {
        openBlock("text", { type: "text", text: "" });
        emit("content_block_delta", {
          index: currentIndex,
          delta: { type: "text_delta", text: batch.incompleteText },
        });
      }

      for (const t of batch.executable) {

        const idx = openBlock("tool", {
          type: "tool_use",
          id: t.id,
          name: t.name,
          input: {},
        });
        // 参数一次性发出（已校验为合法 JSON）
        if (t.arguments !== "") {
          emit("content_block_delta", {
            index: idx,
            delta: { type: "input_json_delta", partial_json: t.arguments },
          });
        }
        emittedToolBlocks += 1;
      }

      closeBlock(); // 不变量 5

      // 正常收尾
      // 用「实际发出的块数」而不是「见过 tool delta」——否则会出现
      // stop_reason:tool_use 但 content 里零个 tool_use 块，客户端等一个永不到来的调用
      stopReason = mapStopReason(state.finishReason, emittedToolBlocks > 0);
      if (outcome === "upstream_finish_length") stopReason = "max_tokens";
      if (outcome === "upstream_content_filter") stopReason = "refusal";

      // stop_sequence 回填：上游不告诉我们命中了哪个，只能在 length 之外的正常停止时留 null
      if (stopReason === "stop_sequence" && opts.stopSequences.length > 0) {
        stopSequence = opts.stopSequences[0] ?? null;
      }

      emit("message_delta", {
        delta: {
          stop_reason: stopReason,
          stop_sequence: stopSequence,
          // Anthropic 现行协议：refusal 终态要带 stop_details
          ...(stopReason === "refusal"
            ? { stop_details: { type: "refusal", category: null, explanation: null } }
            : {}),
        },
        usage: anthropicUsage(usage),
      });
      emit("message_stop", {});

      try { controller.close(); } catch { /* already closed */ }
    },

    cancel() {
      state.downstreamCanceled = true;
    },
  });
}

/** 上游 usage → Anthropic usage（含 cache 字段） */
export function anthropicUsage(u: UpstreamUsage | null): Record<string, number> {
  if (!u) return { input_tokens: 0, output_tokens: 0 };
  const out: Record<string, number> = {
    input_tokens: u.prompt_tokens,
    output_tokens: u.completion_tokens,
  };
  if (u.cache_write_input_tokens > 0) out["cache_creation_input_tokens"] = u.cache_write_input_tokens;
  if (u.cache_read_input_tokens > 0) out["cache_read_input_tokens"] = u.cache_read_input_tokens;
  return out;
}

export function anthropicErrorTypeFor(outcome: string): string {
  switch (outcome) {
    case "upstream_error_quota":
    case "upstream_error_rate_limit": return "rate_limit_error";
    case "upstream_error_invalid_request": return "invalid_request_error";
    case "upstream_error_timeout":
    case "upstream_idle_timeout": return "timeout_error";
    case "upstream_sensitive_content": return "api_error";
    default: return "api_error";
  }
}

export function failureMessage(outcome: string, errorEvent: string): string {
  if (errorEvent !== "") {
    const formatted = formatUpstreamErrorEvent(errorEvent);
    const trimmed = formatted.length > 4096 ? `${formatted.slice(0, 4096)} […truncated]` : formatted;
    return `upstream error: ${trimmed}`;
  }
  switch (outcome) {
    case "upstream_eof_without_terminal":
      return "upstream stream ended without a terminal event";
    case "upstream_protocol_error":
      return "upstream sent a malformed output event";
    case "upstream_idle_timeout":
      return "upstream stream stalled";
    case "upstream_sensitive_content":
      return "upstream rejected the request as sensitive content";
    default:
      return `upstream stream failed (${outcome})`;
  }
}
