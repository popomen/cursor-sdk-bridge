/**
 * 上游统一事件流 → Anthropic Messages 完整 JSON 响应（非流式）。
 */

import type { UpstreamUsage } from "../types/unified.js";
import { statusForFailureOutcome } from "../http/errors.js";
import { classifyOutcome, isFailureOutcome, newStreamState, runUpstreamStream } from "../upstream/engine.js";
import type { StreamHooks } from "../upstream/engine.js";
import { mapStopReason, randomHex } from "./anthropic-request.js";
import { anthropicUsage, encodeReasoningSignature, failureMessage, anthropicErrorTypeFor } from "./anthropic-stream.js";
import { validateToolBatch } from "./tool-schema.js";

export interface AnthropicMessageResult {
  ok: true;
  body: Record<string, unknown>;
  usage: UpstreamUsage | null;
  outcome: string;
}
export interface AnthropicMessageFailure {
  ok: false;
  status: number;
  errorType: string;
  message: string;
  outcome: string;
}

export async function collectAnthropicMessage(
  upstreamBody: ReadableStream<Uint8Array>,
  requestedModel: string,
  idleMs: number,
  signal?: AbortSignal,
  hooks?: StreamHooks,
): Promise<AnthropicMessageResult | AnthropicMessageFailure> {
  const state = newStreamState();
  const tools = state.toolCalls;

  let text = "";
  let thinking = "";
  let usage: UpstreamUsage | null = null;

  for await (const ev of runUpstreamStream(upstreamBody, state, idleMs, signal, hooks)) {
    if (signal?.aborted) {
      state.downstreamCanceled = true;
      break;
    }
    if (ev.type === "delta") {
      text += ev.output.response;
      thinking += ev.output.reasoningContent;
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
      errorType: anthropicErrorTypeFor(outcome),
      message: failureMessage(outcome, state.errorEvent),
      outcome,
    };
  }

  const content: unknown[] = [];
  if (thinking !== "") {
    // 签名必须非空：Anthropic SDK 会在本地校验，空签名的 thinking 块在回传时会 400。
    // 与流式路径用同一个编码函数，保证两条路径行为一致。
    content.push({ type: "thinking", thinking, signature: encodeReasoningSignature(thinking) });
  }
  if (text !== "") {
    content.push({ type: "text", text });
  }
  // 与流式路径同样的校验：残缺的工具调用绝不能包装成可执行的 tool_use。
  // 之前解析失败会退化成 input:{_raw:"..."} —— 那等于凭空造了一次语义完全
  // 不同的调用，客户端会真的去执行它。
  const truncatedTerminal = outcome === "upstream_finish_length" || outcome === "upstream_content_filter";
  let emittedToolBlocks = 0;
  const candidates = tools.sorted().map((tool) => ({
    id: tool.id,
    name: tool.name,
    type: tool.type,
    arguments: tool.arguments,
  }));
  const batch = validateToolBatch(candidates, truncatedTerminal);
  if (batch.fatal) {
    return {
      ok: false,
      status: 502,
      errorType: "api_error",
      message: `tool call ${batch.fatal.name}: ${batch.fatal.reason}`,
      outcome,
    };
  }
  if (batch.incompleteText !== "") content.push({ type: "text", text: batch.incompleteText });

  for (const t of batch.executable) {

    content.push({
      type: "tool_use",
      id: t.id,
      name: t.name,
      input: t.arguments === "" ? {} : (JSON.parse(t.arguments) as unknown),
    });
    emittedToolBlocks += 1;
  }
  // 所有 block 都空时兜底，保持 schema 合法
  if (content.length === 0) content.push({ type: "text", text: "" });

  // 以实际产出的 tool_use 块为准，而不是「累积器里有几个桶」——
  // 桶可能只有参数分片、构不成合法调用
  let stopReason = mapStopReason(state.finishReason, emittedToolBlocks > 0);
  if (outcome === "upstream_finish_length") stopReason = "max_tokens";
  if (outcome === "upstream_content_filter") stopReason = "refusal";

  return {
    ok: true,
    outcome,
    usage,
    body: {
      id: `msg_${randomHex(24)}`,
      type: "message",
      role: "assistant",
      model: requestedModel,
      content,
      stop_reason: stopReason,
      stop_sequence: null,
      ...(stopReason === "refusal"
        ? { stop_details: { type: "refusal", category: null, explanation: null } }
        : {}),
      usage: anthropicUsage(usage),
    },
  };
}
