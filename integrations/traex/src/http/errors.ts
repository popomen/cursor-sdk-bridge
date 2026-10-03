/**
 * 错误响应。**按端点协议区分格式** —— Anthropic 客户端会解析 Anthropic 信封，
 * 给它 OpenAI 形状的错误体会让它把错误当成未知响应。
 */

import type { UpstreamErrorCategory } from "../upstream/events.js";

export type Protocol = "anthropic" | "openai";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number = 500,
    readonly errorType: string = "api_error",
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Anthropic 官方错误信封 */
export function anthropicErrorBody(type: string, message: string): Record<string, unknown> {
  return { type: "error", request_id: null, error: { type, message } };
}

/** OpenAI 错误信封 */
export function openaiErrorBody(
  message: string,
  type: string,
  code?: string,
): Record<string, unknown> {
  return {
    error: {
      message,
      type,
      param: null,
      ...(code !== undefined ? { code } : {}),
    },
  };
}

export function errorBodyFor(
  protocol: Protocol,
  status: number,
  message: string,
  errorType?: string,
  code?: string,
): Record<string, unknown> {
  if (protocol === "anthropic") {
    return anthropicErrorBody(errorType ?? anthropicTypeForStatus(status), message);
  }
  return openaiErrorBody(message, errorType ?? openaiTypeForStatus(status), code);
}

export function anthropicTypeForStatus(status: number): string {
  switch (status) {
    case 400: return "invalid_request_error";
    case 401: return "authentication_error";
    case 403: return "permission_error";
    case 404: return "not_found_error";
    case 413: return "request_too_large";
    case 429: return "rate_limit_error";
    case 529: return "overloaded_error";
    default: return status >= 500 ? "api_error" : "invalid_request_error";
  }
}

export function openaiTypeForStatus(status: number): string {
  switch (status) {
    case 400: return "invalid_request_error";
    case 401:
    case 403: return "authentication_error";
    case 404: return "invalid_request_error";
    case 413: return "invalid_request_error";
    case 429: return "rate_limit_error";
    default: return status >= 500 ? "server_error" : "invalid_request_error";
  }
}

/**
 * 流内失败 outcome → 非流式下游 HTTP 状态。
 * 4008 额度 / 4050 队列满 → 429；4001/4037 与超窗 → 400；其余失败保持 502。
 */
export function statusForFailureOutcome(outcome: string): number {
  switch (outcome) {
    case "upstream_error_quota":
    case "upstream_error_rate_limit":
      return 429;
    case "upstream_error_invalid_request":
      return 400;
    default:
      return 502;
  }
}

export function openaiTypeForFailureOutcome(outcome: string): string {
  switch (outcome) {
    case "upstream_error_quota":
      return "insufficient_quota";
    case "upstream_error_rate_limit":
      return "rate_limit_error";
    case "upstream_error_invalid_request":
      return "invalid_request_error";
    default:
      return "server_error";
  }
}

/**
 * HTTP 握手失败时，仅在识别到已知业务码 / 超窗时覆盖上游状态；
 * 其余情况沿用上游 HTTP 状态（非法值回落 502）。
 */
export function statusForUpstreamErrorCategory(
  category: UpstreamErrorCategory,
  fallbackStatus: number,
): number {
  switch (category) {
    case "quota":
    case "rate_limit":
      return 429;
    case "invalid_request":
      return 400;
    default:
      return fallbackStatus >= 400 && fallbackStatus < 600 ? fallbackStatus : 502;
  }
}

/** 请求路径属于哪个协议族 */
export function protocolForPath(path: string): Protocol {
  const p = path.split("?")[0] ?? "";
  if (p === "/v1/messages" || p === "/messages" ||
      p === "/v1/messages/count_tokens" || p === "/messages/count_tokens") {
    return "anthropic";
  }
  return "openai";
}
