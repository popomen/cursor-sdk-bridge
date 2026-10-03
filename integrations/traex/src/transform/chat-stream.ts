/**
 * 上游统一事件流 → OpenAI Chat Completions（流式 chunk / 完整 JSON）。
 *
 * 两处超出协议最小实现的地方：
 *  - 非流式也保留 reasoning（放在 `message.reasoning_content` 扩展字段）
 *  - 支持 `stream_options.include_usage`，在 `[DONE]` 前发一个带 usage 的空 choices chunk
 */

import { openaiTypeForFailureOutcome, statusForFailureOutcome } from "../http/errors.js";
import type { UpstreamUsage } from "../types/unified.js";
import { classifyOutcome, isFailureOutcome, newStreamState, runUpstreamStream } from "../upstream/engine.js";
import type { StreamHooks } from "../upstream/engine.js";
import { randomHex } from "./anthropic-request.js";
import { failureMessage } from "./anthropic-stream.js";
import { validateToolBatch } from "./tool-schema.js";
import { STRUCTURED_OUTPUT_TOOL_NAME } from "./upstream-body.js";

const encoder = new TextEncoder();

function chatUsage(u: UpstreamUsage | null): Record<string, unknown> {
  if (!u) return { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
  const out: Record<string, unknown> = {
    prompt_tokens: u.prompt_tokens,
    completion_tokens: u.completion_tokens,
    total_tokens: u.total_tokens,
  };
  if (u.reasoning_tokens > 0) {
    out["completion_tokens_details"] = { reasoning_tokens: u.reasoning_tokens };
  }
  if (u.cache_read_input_tokens > 0) {
    out["prompt_tokens_details"] = { cached_tokens: u.cache_read_input_tokens };
  }
  return out;
}

function finishReasonFor(upstream: string, hasTools: boolean): string {
  const fr = upstream.trim().toLowerCase();
  if (hasTools && (fr === "" || fr === "stop" || fr === "end_turn" || fr === "complete" || fr === "completed")) {
    return "tool_calls";
  }
  if (fr === "") return "stop";
  if (fr === "stop" || fr === "end_turn" || fr === "complete" || fr === "completed") return "stop";
  if (fr === "tool_calls" || fr === "tool_use") return hasTools ? "tool_calls" : "stop";
  if (fr === "length" || fr === "max_tokens" || fr === "max_output_tokens") return "length";
  if (fr === "content_filter" || fr === "content_filtered") return "content_filter";
  if (fr === "sensitive_content" || fr === "sensitive") return "content_filter";
  // Engine 只会把白名单内的值交到正常收尾；这里仍 fail closed，不能把未来新增值
  // 原样泄露成 OpenAI 枚举之外的 finish_reason。
  return "stop";
}

export interface ChatStreamOptions {
  upstreamBody: ReadableStream<Uint8Array>;
  requestedModel: string;
  includeUsage: boolean;
  idleMs: number;
  signal?: AbortSignal;
  /** 当前上游流生命周期回调（排队日志 / 用量统计） */
  hooks?: StreamHooks;
  /** 把合成 structured_output 调用还原为 assistant JSON 文本。 */
  structuredOutput?: boolean;
}

export function chatSseStream(opts: ChatStreamOptions): ReadableStream<Uint8Array> {
  const state = newStreamState();

  return new ReadableStream<Uint8Array>({
    async start(controller) {
      const id = `chatcmpl-${randomHex(24)}`;
      const created = Math.floor(Date.now() / 1000);
      let closed = false;

      const push = (s: string): boolean => {
        if (closed) return false;
        try {
          controller.enqueue(encoder.encode(s));
          return true;
        } catch {
          closed = true;
          return false;
        }
      };

      const sendChunk = (delta: Record<string, unknown>, finishReason: string | null): void => {
        push(
          `data: ${JSON.stringify({
            id,
            object: "chat.completion.chunk",
            created,
            model: opts.requestedModel,
            choices: [{ index: 0, delta, finish_reason: finishReason }],
          })}\n\n`,
        );
      };

      const tools = state.toolCalls;
      let sentRole = false;
      let usage: UpstreamUsage | null = null;

      try {
        for await (const ev of runUpstreamStream(opts.upstreamBody, state, opts.idleMs, opts.signal, opts.hooks)) {
          if (opts.signal?.aborted) {
            state.downstreamCanceled = true;
            break;
          }

          if (ev.type === "queue") {
            push(`: trae-queue ${JSON.stringify(ev.queue)}\n\n`);
            continue;
          }
          if (ev.type === "usage") {
            usage = ev.usage;
            continue;
          }
          if (ev.type === "terminal" || ev.type === "errorEvent" || ev.type === "protocolError" || ev.type === "readError") {
            break;
          }
          if (ev.type !== "delta") continue;

          const delta: Record<string, unknown> = {};
          const out = ev.output;

          if (out.reasoningContent !== "") delta["reasoning_content"] = out.reasoningContent;
          if (out.response !== "") delta["content"] = out.response;

          // 工具参数必须等终态后整批校验；流中先发出去就无法在发现半截 JSON 时撤回。

          if (Object.keys(delta).length === 0) continue;

          if (!sentRole) {
            delta["role"] = "assistant";
            sentRole = true;
          }
          sendChunk(delta, null);
        }
      } catch (e) {
        state.readError = e as Error;
      }

      const outcome = classifyOutcome(state);

      if (outcome === "downstream_canceled" || opts.signal?.aborted) {
        try { controller.close(); } catch { /* ignore */ }
        return;
      }

      if (isFailureOutcome(outcome)) {
        // 流已开始，只能把错误塞进流里
        push(
          `data: ${JSON.stringify({
            error: {
              message: failureMessage(outcome, state.errorEvent),
              type: openaiTypeForFailureOutcome(outcome),
            },
          })}\n\n`,
        );
        push("data: [DONE]\n\n");
        try { controller.close(); } catch { /* ignore */ }
        return;
      }

      const accumulated = tools.sorted().map((tool) => ({
        id: tool.id,
        name: tool.name,
        type: tool.type,
        arguments: tool.arguments,
      }));
      const truncated = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
      const batch = validateToolBatch(accumulated, truncated);
      if (batch.fatal) {
        push(
          `data: ${JSON.stringify({
            error: {
              message: `tool call ${batch.fatal.name}: ${batch.fatal.reason}`,
              type: "server_error",
            },
          })}\n\n`,
        );
        push("data: [DONE]\n\n");
        try { controller.close(); } catch { /* ignore */ }
        return;
      }

      if (batch.incompleteText !== "") {
        const delta: Record<string, unknown> = { content: batch.incompleteText };
        if (!sentRole) {
          delta["role"] = "assistant";
          sentRole = true;
        }
        sendChunk(delta, null);
      }

      const structuredCalls = opts.structuredOutput
        ? batch.executable.filter((tool) => tool.name === STRUCTURED_OUTPUT_TOOL_NAME)
        : [];
      const executableTools = opts.structuredOutput
        ? batch.executable.filter((tool) => tool.name !== STRUCTURED_OUTPUT_TOOL_NAME)
        : batch.executable;

      for (const tool of structuredCalls) {
        const delta: Record<string, unknown> = { content: tool.arguments };
        if (!sentRole) {
          delta["role"] = "assistant";
          sentRole = true;
        }
        sendChunk(delta, null);
      }

      if (executableTools.length > 0) {
        const delta: Record<string, unknown> = {
          tool_calls: executableTools.map((tool, index) => ({
            index,
            id: tool.id,
            type: "function",
            function: { name: tool.name, arguments: tool.arguments === "" ? "{}" : tool.arguments },
          })),
        };
        if (!sentRole) {
          delta["role"] = "assistant";
          sentRole = true;
        }
        sendChunk(delta, null);
      }

      sendChunk({}, finishReasonFor(state.finishReason, executableTools.length > 0));

      if (opts.includeUsage) {
        push(
          `data: ${JSON.stringify({
            id,
            object: "chat.completion.chunk",
            created,
            model: opts.requestedModel,
            choices: [],
            usage: chatUsage(usage),
          })}\n\n`,
        );
      }

      push("data: [DONE]\n\n");
      try { controller.close(); } catch { /* ignore */ }
    },

    cancel() {
      state.downstreamCanceled = true;
    },
  });
}

export interface ChatResult {
  ok: true;
  body: Record<string, unknown>;
  usage: UpstreamUsage | null;
}
export interface ChatFailure {
  ok: false;
  status: number;
  errorType: string;
  message: string;
}

export async function collectChatCompletion(
  upstreamBody: ReadableStream<Uint8Array>,
  requestedModel: string,
  idleMs: number,
  signal?: AbortSignal,
  hooks?: StreamHooks,
  structuredOutput = false,
): Promise<ChatResult | ChatFailure> {
  const state = newStreamState();
  const tools = state.toolCalls;
  let text = "";
  let reasoning = "";
  let usage: UpstreamUsage | null = null;

  for await (const ev of runUpstreamStream(upstreamBody, state, idleMs, signal, hooks)) {
    if (signal?.aborted) {
      state.downstreamCanceled = true;
      break;
    }
    if (ev.type === "delta") {
      text += ev.output.response;
      reasoning += ev.output.reasoningContent;
    } else if (ev.type === "usage") {
      usage = ev.usage;
    } else if (ev.type === "terminal" || ev.type === "errorEvent" || ev.type === "protocolError" || ev.type === "readError") {
      break;
    }
  }

  const outcome = classifyOutcome(state);
  if (isFailureOutcome(outcome)) {
    return {
      ok: false,
      status: statusForFailureOutcome(outcome),
      errorType: openaiTypeForFailureOutcome(outcome),
      message: failureMessage(outcome, state.errorEvent),
    };
  }

  const accumulated = tools.sorted().map((tool) => ({
    id: tool.id,
    name: tool.name,
    type: tool.type,
    arguments: tool.arguments,
  }));
  const truncated = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
  const batch = validateToolBatch(accumulated, truncated);
  if (batch.fatal) {
    return {
      ok: false,
      status: 502,
      errorType: "server_error",
      message: `tool call ${batch.fatal.name}: ${batch.fatal.reason}`,
    };
  }
  const structuredCalls = structuredOutput
    ? batch.executable.filter((tool) => tool.name === STRUCTURED_OUTPUT_TOOL_NAME)
    : [];
  const toolCalls = structuredOutput
    ? batch.executable.filter((tool) => tool.name !== STRUCTURED_OUTPUT_TOOL_NAME)
    : batch.executable;
  if (batch.incompleteText !== "") text += text === "" ? batch.incompleteText : `\n${batch.incompleteText}`;
  for (const tool of structuredCalls) {
    text += text === "" ? tool.arguments : `\n${tool.arguments}`;
  }

  const message: Record<string, unknown> = {
    role: "assistant",
    content: text === "" && toolCalls.length > 0 ? null : text,
  };
  // 非流式也保留思维链
  if (reasoning !== "") message["reasoning_content"] = reasoning;
  if (toolCalls.length > 0) {
    message["tool_calls"] = toolCalls.map((t) => ({
      id: t.id,
      type: "function",
      function: { name: t.name, arguments: t.arguments === "" ? "{}" : t.arguments },
    }));
  }

  return {
    ok: true,
    usage,
    body: {
      id: `chatcmpl-${randomHex(24)}`,
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: requestedModel,
      choices: [
        {
          index: 0,
          message,
          logprobs: null,
          finish_reason: finishReasonFor(state.finishReason, toolCalls.length > 0),
        },
      ],
      usage: chatUsage(usage),
    },
  };
}
