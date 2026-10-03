/**
 * 上游统一事件流 → OpenAI Responses SSE。
 *
 * 编号规则（最容易错的部分）：
 *  - `output_index`：单一计数器，按**首次出现顺序**分配给 reasoning 块 / message 块 /
 *    每个 tool call。分配后固定不变。
 *  - `content_index` / `summary_index`：恒为 0。
 *  - `sequence_number`：全局自增，从 0 开始，**每个发出的事件 +1**。
 *  - `item_id` 前缀按类型：msg_ / rs_ / fc_ / ctc_ / tsc_
 *
 * 同一个上游 delta 内的处理顺序固定为 reasoning → text → tools，
 * 所以同一帧里首次出现时 reasoning 会拿到更小的 output_index。
 *
 * 收尾顺序：reasoning 的 done 事件 → message 的 done 事件 → 所有 tool 的 done 事件
 * （按上游 index 升序），最后才是终止事件。
 */

import { ApiError, openaiTypeForFailureOutcome, statusForFailureOutcome } from "../http/errors.js";
import type { ResponsesEventSource } from "../upstream/admission.js";
import type { UpstreamUsage } from "../types/unified.js";
import { classifyOutcome, newStreamState, runUpstreamStream } from "../upstream/engine.js";
import type { StreamHooks } from "../upstream/engine.js";
import { randomHex } from "./anthropic-request.js";
import { failureMessage } from "./anthropic-stream.js";
import { unwrapCustomInput, type ToolMetaMap } from "./responses-request.js";
import { validateToolBatch } from "./tool-schema.js";
import { STRUCTURED_OUTPUT_TOOL_NAME } from "./upstream-body.js";

const encoder = new TextEncoder();
const QUEUE_KEEPALIVE_INTERVAL_MS = 10_000;

function responsesUsage(u: UpstreamUsage | null): Record<string, unknown> {
  // Codex 在 input_tokens 缺失时会拒绝解析 response.completed，所以四个键永远存在
  const base = {
    input_tokens: u?.prompt_tokens ?? 0,
    output_tokens: u?.completion_tokens ?? 0,
    total_tokens: u?.total_tokens ?? 0,
    input_tokens_details: {
      cached_tokens: u?.cache_read_input_tokens ?? 0,
      cache_write_tokens: u?.cache_write_input_tokens ?? 0,
    },
    output_tokens_details: { reasoning_tokens: u?.reasoning_tokens ?? 0 },
  };
  return base;
}

interface ToolState {
  upstreamIndex: number;
  outputIndex: number;
  itemId: string;
  callId: string;
  name: string;
  /** function | custom | tool_search */
  kind: string;
  namespace: string;
  added: boolean;
  done: boolean;
}

function toolItemPayload(st: ToolState, status: string, args: string): Record<string, unknown> {
  const type =
    st.kind === "custom" ? "custom_tool_call" : st.kind === "tool_search" ? "tool_search_call" : "function_call";

  const item: Record<string, unknown> = {
    id: st.itemId,
    type,
    status,
    call_id: st.callId,
  };
  if (st.namespace !== "") item["namespace"] = st.namespace;

  if (type === "custom_tool_call") {
    item["name"] = st.name;
    item["input"] = unwrapCustomInput(args);
  } else if (type === "tool_search_call") {
    item["execution"] = "client";
    let parsed: unknown = {};
    try {
      parsed = args === "" ? {} : JSON.parse(args);
    } catch {
      parsed = {};
    }
    item["arguments"] = parsed;
    // tool_search_call item 不带 name
  } else {
    item["name"] = st.name;
    item["arguments"] = status === "in_progress" ? args : args === "" ? "{}" : args;
  }
  return item;
}

export interface ResponsesStreamOptions {
  upstreamBody?: ReadableStream<Uint8Array>;
  /** A single logical event source may coordinate several rejected admission attempts. */
  eventSource?: ResponsesEventSource;
  requestedModel: string;
  toolMeta: ToolMetaMap;
  idleMs: number;
  signal?: AbortSignal;
  /** 当前上游流生命周期回调（排队日志 / 用量统计） */
  hooks?: StreamHooks;
  /** 把合成 structured_output 调用还原为 assistant JSON 文本。 */
  structuredOutput?: boolean;
  deferLifecycle?: boolean;
}

export function responsesSseStream(opts: ResponsesStreamOptions): ReadableStream<Uint8Array> {
  if (!opts.upstreamBody && !opts.eventSource) throw new Error("Responses requires an upstream event source");
  const state = newStreamState();
  const canceled = new AbortController();
  const signal = opts.signal ? AbortSignal.any([opts.signal, canceled.signal]) : canceled.signal;

  return new ReadableStream<Uint8Array>({
    async start(controller) {
      const responseId = `resp_${randomHex(24)}`;
      const createdAt = Math.floor(Date.now() / 1000);
      let closed = false;
      let sequence = 0;
      let lastEventAt = performance.now();

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
      const emit = (type: string, payload: Record<string, unknown>): void => {
        const data = { type, sequence_number: sequence++, ...payload };
        if (push(encoder.encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`))) {
          lastEventAt = performance.now();
        }
      };

      const baseResponse = (status: string): Record<string, unknown> => ({
        id: responseId,
        object: "response",
        created_at: createdAt,
        status,
        model: opts.requestedModel,
        output: [],
      });

      let nextOutputIndex = 0;
      const messageItemId = `msg_${randomHex(24)}`;
      const reasoningItemId = `rs_${randomHex(24)}`;

      let reasoningIndex = -1;
      let reasoningPartAdded = false;
      let reasoningText = "";

      let textIndex = -1;
      let textPartAdded = false;
      let textContent = "";

      const tools = state.toolCalls;
      const toolStates = new Map<number, ToolState>();

      let usage: UpstreamUsage | null = null;
      let responseStarted = false;

      const startResponse = (): void => {
        if (responseStarted) return;
        responseStarted = true;
        emit("response.created", { response: baseResponse("in_progress") });
        emit("response.in_progress", { response: baseResponse("in_progress") });
      };
      if (!opts.deferLifecycle) startResponse();

      const progressResponse = (): Record<string, unknown> => {
        // Queue events may follow deltas; retain the output already exposed to clients.
        // Tool items remain buffered until terminal validation, so are not public yet.
        const output: Array<{ index: number; item: Record<string, unknown> }> = [];
        if (reasoningIndex >= 0) {
          output.push({
            index: reasoningIndex,
            item: {
              id: reasoningItemId, type: "reasoning", status: "in_progress",
              summary: [{ type: "summary_text", text: reasoningText }],
            },
          });
        }
        if (textIndex >= 0) {
          output.push({
            index: textIndex,
            item: {
              id: messageItemId, type: "message", status: "in_progress", role: "assistant",
              content: [{ type: "output_text", text: textContent, annotations: [] }],
            },
          });
        }
        return {
          ...baseResponse("in_progress"),
          output: output.sort((a, b) => a.index - b.index).map(({ item }) => item),
        };
      };

      const ensureToolState = (upstreamIndex: number): ToolState => {
        let st = toolStates.get(upstreamIndex);
        if (!st) {
          st = {
            upstreamIndex,
            outputIndex: -1,
            itemId: "",
            callId: "",
            name: "",
            kind: "function",
            namespace: "",
            added: false,
            done: false,
          };
          toolStates.set(upstreamIndex, st);
        }
        return st;
      };

      /** 工具信息齐全才能开 item */
      const toolReady = (st: ToolState): boolean => st.callId !== "" && st.name !== "";

      const emitToolAdded = (st: ToolState): void => {
        if (st.added || !toolReady(st)) return;
        startResponse();
        if (st.outputIndex < 0) st.outputIndex = nextOutputIndex++;
        if (st.itemId === "") {
          const hex = randomHex(24);
          st.itemId =
            st.kind === "custom" ? `ctc_${hex}` : st.kind === "tool_search" ? `tsc_${hex}` : `fc_${hex}`;
        }
        st.added = true;
        emit("response.output_item.added", {
          output_index: st.outputIndex,
          item: toolItemPayload(st, "in_progress", ""),
        });
      };

      const ensureTextPart = (): void => {
        startResponse();
        if (textIndex < 0) {
          textIndex = nextOutputIndex++;
          emit("response.output_item.added", {
            output_index: textIndex,
            item: { id: messageItemId, type: "message", status: "in_progress", role: "assistant", content: [] },
          });
        }
        if (!textPartAdded) {
          textPartAdded = true;
          emit("response.content_part.added", {
            item_id: messageItemId,
            output_index: textIndex,
            content_index: 0,
            part: { type: "output_text", text: "", annotations: [] },
          });
        }
      };

      const appendTextDelta = (delta: string): void => {
        if (delta === "") return;
        ensureTextPart();
        textContent += delta;
        emit("response.output_text.delta", {
          item_id: messageItemId,
          output_index: textIndex,
          content_index: 0,
          delta,
        });
      };

      try {
        const events = opts.eventSource
          ? opts.eventSource(state, signal, opts.hooks, (progress) => {
            if (signal.aborted || closed) return;
            push(encoder.encode(`: trae-admission ${JSON.stringify(progress)}\n\n`));
            if (responseStarted) emit("response.in_progress", { response: progressResponse() });
          })
          : runUpstreamStream(opts.upstreamBody!, state, opts.idleMs, signal, opts.hooks);
        for await (const ev of events) {
          if (signal.aborted) {
            state.downstreamCanceled = true;
            break;
          }
          if (ev.type === "queue") {
            push(encoder.encode(`: trae-queue ${JSON.stringify(ev.queue)}\n\n`));
            // Codex times out waiting for parsed SSE events; comments do not count.
            // Only refresh on upstream activity, never on an unconditional timer.
            if (responseStarted && performance.now() - lastEventAt >= QUEUE_KEEPALIVE_INTERVAL_MS) {
              emit("response.in_progress", { response: progressResponse() });
            }
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

          const out = ev.output;

          // ---- reasoning ----
          if (out.reasoningContent !== "") {
            startResponse();
            if (reasoningIndex < 0) {
              reasoningIndex = nextOutputIndex++;
              emit("response.output_item.added", {
                output_index: reasoningIndex,
                item: { id: reasoningItemId, type: "reasoning", status: "in_progress", summary: [] },
              });
            }
            if (!reasoningPartAdded) {
              reasoningPartAdded = true;
              emit("response.reasoning_summary_part.added", {
                item_id: reasoningItemId,
                output_index: reasoningIndex,
                summary_index: 0,
                part: { type: "summary_text", text: "" },
              });
            }
            reasoningText += out.reasoningContent;
            emit("response.reasoning_summary_text.delta", {
              item_id: reasoningItemId,
              output_index: reasoningIndex,
              summary_index: 0,
              delta: out.reasoningContent,
            });
          }

          // ---- text ----
          appendTextDelta(out.response);

          // ---- tools ----
          for (const d of out.toolCalls) {
            const acc = tools.get(d.index)!;
            if (opts.structuredOutput && acc.name === STRUCTURED_OUTPUT_TOOL_NAME) continue;
            const st = ensureToolState(d.index);
            if (acc.id !== "") st.callId = acc.id;
            if (acc.name !== "") {
              st.name = acc.name;
              const meta = opts.toolMeta.get(acc.name);
              if (meta) {
                st.kind = meta.custom ? "custom" : meta.kind === "tool_search" ? "tool_search" : "function";
                st.namespace = meta.namespace;
                st.name = meta.name; // 还原成客户端认识的原名
              }
            }
            // 工具 item 与参数必须等终态后整批校验，避免已经发出后才发现同批另一个
            // 调用残缺。文本/推理仍保持实时流式。
          }
        }
      } catch (e) {
        state.readError = e as Error;
      }

      const outcome = classifyOutcome(state);

      if (outcome === "downstream_canceled" || signal.aborted) {
        try { controller.close(); } catch { /* ignore */ }
        return;
      }

      const failWith = (code: string, message: string, type = "server_error"): void => {
        startResponse();
        if (code === "rate_limit_exceeded" && !/try again in\s*\d+/i.test(message)) {
          message += " Please try again in 60 seconds.";
        }
        emit("response.failed", {
          response: {
            ...baseResponse("failed"),
            error: { type, code, message },
          },
        });
        try { controller.close(); } catch { /* ignore */ }
      };

      if (state.readError instanceof ApiError) {
        failWith(state.readError.code ?? "upstream_stream_error", state.readError.message, state.readError.errorType);
        return;
      }
      if (
        outcome === "upstream_error_event" ||
        outcome === "upstream_error_timeout" ||
        outcome === "upstream_error_rate_limit" ||
        outcome === "upstream_error_quota" ||
        outcome === "upstream_error_invalid_request"
      ) {
        const code = outcome === "upstream_error_rate_limit" ? "rate_limit_exceeded"
          : outcome === "upstream_error_quota" ? "insufficient_quota"
          : outcome === "upstream_error_invalid_request" ? "invalid_prompt" : "upstream_stream_error";
        failWith(code, failureMessage(outcome, state.errorEvent), openaiTypeForFailureOutcome(outcome));
        return;
      }
      if (outcome === "upstream_protocol_error") {
        failWith("upstream_protocol_error", "upstream sent a malformed output event");
        return;
      }
      if (outcome === "upstream_idle_timeout" || outcome === "upstream_read_error") {
        failWith("upstream_stream_read_error", state.readError?.message ?? "upstream read error");
        return;
      }
      if (outcome === "upstream_eof_without_terminal") {
        failWith("upstream_stream_incomplete", "upstream stream ended without a terminal event");
        return;
      }
      // sensitive_content 判为 failed 而不是 incomplete —— 语义上它可重试
      if (outcome === "upstream_sensitive_content") {
        failWith("upstream_sensitive_content", "upstream rejected the request as sensitive content");
        return;
      }

      const accumulatedTools = tools.sorted();
      const candidates = accumulatedTools.map((tool) => {
        const meta = opts.toolMeta.get(tool.name);
        const kind = meta?.custom ? "custom" : meta?.kind === "tool_search" ? "tool_search" : "function";
        return {
          id: tool.id,
          name: meta?.name ?? tool.name,
          type: kind,
          arguments: tool.arguments,
        };
      });
      const truncated = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
      const batch = validateToolBatch(candidates, truncated);
      if (batch.fatal) {
        failWith(
          "upstream_tool_call_incomplete",
          `tool call ${batch.fatal.name}: ${batch.fatal.reason}`,
        );
        return;
      }
      if (batch.incompleteText !== "") {
        appendTextDelta(textContent === "" ? batch.incompleteText : `\n${batch.incompleteText}`);
      }

      const structuredCalls = opts.structuredOutput && batch.executable.length > 0
        ? accumulatedTools.filter((tool) => tool.name === STRUCTURED_OUTPUT_TOOL_NAME)
        : [];
      const executableTools = batch.executable.length > 0
        ? accumulatedTools.filter((tool) => !opts.structuredOutput || tool.name !== STRUCTURED_OUTPUT_TOOL_NAME)
        : [];
      for (const tool of structuredCalls) {
        appendTextDelta(textContent === "" ? tool.arguments : `\n${tool.arguments}`);
      }

      // 只有整批通过校验才开始对下游公开工具调用。
      if (executableTools.length > 0) {
        for (const tool of executableTools) {
          const st = toolStates.get(tool.index);
          if (!st) continue;
          emitToolAdded(st);
          if (!st.added || tool.arguments === "") continue;
          if (st.kind === "function") {
            emit("response.function_call_arguments.delta", {
              item_id: st.itemId,
              output_index: st.outputIndex,
              delta: tool.arguments,
            });
          } else if (st.kind === "custom") {
            emit("response.custom_tool_call_input.delta", {
              item_id: st.itemId,
              output_index: st.outputIndex,
              delta: unwrapCustomInput(tool.arguments),
            });
          }
        }
      }

      // 空输出也要走 added → part added → done → item done，不能只在终态对象里凭空出现。
      if (nextOutputIndex === 0) ensureTextPart();

      // ---- 收尾：reasoning → message → tools ----
      if (reasoningPartAdded) {
        emit("response.reasoning_summary_text.done", {
          item_id: reasoningItemId,
          output_index: reasoningIndex,
          summary_index: 0,
          text: reasoningText,
        });
        emit("response.reasoning_summary_part.done", {
          item_id: reasoningItemId,
          output_index: reasoningIndex,
          summary_index: 0,
          part: { type: "summary_text", text: reasoningText },
        });
      }
      if (reasoningIndex >= 0) {
        emit("response.output_item.done", {
          output_index: reasoningIndex,
          item: {
            id: reasoningItemId,
            type: "reasoning",
            status: "completed",
            summary: [{ type: "summary_text", text: reasoningText }],
          },
        });
      }

      if (textPartAdded) {
        emit("response.output_text.done", {
          item_id: messageItemId,
          output_index: textIndex,
          content_index: 0,
          text: textContent,
        });
        emit("response.content_part.done", {
          item_id: messageItemId,
          output_index: textIndex,
          content_index: 0,
          part: { type: "output_text", text: textContent, annotations: [] },
        });
      }
      if (textIndex >= 0) {
        emit("response.output_item.done", {
          output_index: textIndex,
          item: {
            id: messageItemId,
            type: "message",
            status: "completed",
            role: "assistant",
            content: [{ type: "output_text", text: textContent, annotations: [] }],
          },
        });
      }

      const outputItems: unknown[] = new Array(nextOutputIndex);
      if (reasoningIndex >= 0) {
        outputItems[reasoningIndex] = {
          id: reasoningItemId,
          type: "reasoning",
          status: "completed",
          summary: [{ type: "summary_text", text: reasoningText }],
        };
      }
      if (textIndex >= 0) {
        outputItems[textIndex] = {
          id: messageItemId,
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "output_text", text: textContent, annotations: [] }],
        };
      }

      for (const st of [...toolStates.values()].sort((a, b) => a.upstreamIndex - b.upstreamIndex)) {
        if (executableTools.length === 0) continue;
        const acc = tools.get(st.upstreamIndex);
        const args = acc?.arguments ?? "";
        emitToolAdded(st); // 兜底补发
        if (!st.added) continue;
        if (st.kind === "function") {
          emit("response.function_call_arguments.done", {
            item_id: st.itemId,
            output_index: st.outputIndex,
            arguments: args === "" ? "{}" : args,
          });
        } else if (st.kind === "custom") {
          emit("response.custom_tool_call_input.done", {
            item_id: st.itemId,
            output_index: st.outputIndex,
            input: unwrapCustomInput(args),
          });
        }
        st.done = true;
        const item = toolItemPayload(st, "completed", args);
        emit("response.output_item.done", { output_index: st.outputIndex, item });
        outputItems[st.outputIndex] = item;
      }

      if (outcome === "upstream_finish_length") {
        emit("response.incomplete", {
          response: {
            ...baseResponse("incomplete"),
            output: outputItems,
            incomplete_details: { reason: "max_output_tokens" },
            usage: responsesUsage(usage),
          },
        });
      } else if (outcome === "upstream_content_filter") {
        emit("response.incomplete", {
          response: {
            ...baseResponse("incomplete"),
            output: outputItems,
            incomplete_details: { reason: "content_filter" },
            usage: responsesUsage(usage),
          },
        });
      } else {
        emit("response.completed", {
          response: {
            ...baseResponse("completed"),
            output: outputItems,
            usage: responsesUsage(usage),
          },
        });
      }

      try { controller.close(); } catch { /* ignore */ }
    },

    cancel() {
      state.downstreamCanceled = true;
      canceled.abort(new Error("downstream canceled"));
    },
  });
}

export interface ResponsesResult {
  ok: true;
  body: Record<string, unknown>;
  usage: UpstreamUsage | null;
}
export interface ResponsesFailure {
  ok: false;
  status: number;
  errorType: string;
  message: string;
}

export async function collectResponsesMessage(
  upstreamBody: ReadableStream<Uint8Array>,
  requestedModel: string,
  toolMeta: ToolMetaMap,
  idleMs: number,
  signal?: AbortSignal,
  hooks?: StreamHooks,
  structuredOutput = false,
): Promise<ResponsesResult | ResponsesFailure> {
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
  if (outcome !== "completed" && outcome !== "upstream_finish_length" && outcome !== "upstream_content_filter") {
    return {
      ok: false,
      status: statusForFailureOutcome(outcome),
      errorType: openaiTypeForFailureOutcome(outcome),
      message: failureMessage(outcome, state.errorEvent),
    };
  }

  const accumulatedTools = tools.sorted();
  const candidates = accumulatedTools.map((tool) => {
    const meta = toolMeta.get(tool.name);
    return {
      id: tool.id,
      name: meta?.name ?? tool.name,
      type: meta?.custom ? "custom" : meta?.kind === "tool_search" ? "tool_search" : "function",
      arguments: tool.arguments,
    };
  });
  const truncated = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
  const batch = validateToolBatch(candidates, truncated);
  if (batch.fatal) {
    return {
      ok: false,
      status: 502,
      errorType: "server_error",
      message: `tool call ${batch.fatal.name}: ${batch.fatal.reason}`,
    };
  }
  if (batch.incompleteText !== "") text += text === "" ? batch.incompleteText : `\n${batch.incompleteText}`;
  const structuredCalls = structuredOutput && batch.executable.length > 0
    ? accumulatedTools.filter((tool) => tool.name === STRUCTURED_OUTPUT_TOOL_NAME)
    : [];
  const executableTools = batch.executable.length > 0
    ? accumulatedTools.filter((tool) => !structuredOutput || tool.name !== STRUCTURED_OUTPUT_TOOL_NAME)
    : [];
  for (const tool of structuredCalls) {
    text += text === "" ? tool.arguments : `\n${tool.arguments}`;
  }

  const output: unknown[] = [];
  if (reasoning !== "") {
    output.push({
      id: `rs_${randomHex(24)}`,
      type: "reasoning",
      status: "completed",
      summary: [{ type: "summary_text", text: reasoning }],
    });
  }
  if (text !== "") {
    output.push({
      id: `msg_${randomHex(24)}`,
      type: "message",
      status: "completed",
      role: "assistant",
      content: [{ type: "output_text", text, annotations: [] }],
    });
  }
  for (const t of executableTools) {
    const meta = toolMeta.get(t.name);
    const kind = meta?.custom ? "custom" : meta?.kind === "tool_search" ? "tool_search" : "function";
    const st: ToolState = {
      upstreamIndex: t.index,
      outputIndex: 0,
      itemId: `${kind === "custom" ? "ctc" : kind === "tool_search" ? "tsc" : "fc"}_${randomHex(24)}`,
      callId: t.id,
      name: meta?.name ?? t.name,
      kind,
      namespace: meta?.namespace ?? "",
      added: true,
      done: true,
    };
    output.push(toolItemPayload(st, "completed", t.arguments));
  }
  if (output.length === 0) {
    output.push({
      id: `msg_${randomHex(24)}`,
      type: "message",
      status: "completed",
      role: "assistant",
      content: [{ type: "output_text", text: "", annotations: [] }],
    });
  }

  const status =
    outcome === "upstream_finish_length" || outcome === "upstream_content_filter" ? "incomplete" : "completed";

  return {
    ok: true,
    usage,
    body: {
      id: `resp_${randomHex(24)}`,
      object: "response",
      created_at: Math.floor(Date.now() / 1000),
      status,
      model: requestedModel,
      output,
      ...(status === "incomplete"
        ? {
            incomplete_details: {
              reason: outcome === "upstream_finish_length" ? "max_output_tokens" : "content_filter",
            },
          }
        : {}),
      usage: responsesUsage(usage),
    },
  };
}
