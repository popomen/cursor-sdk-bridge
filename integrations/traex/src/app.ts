/**
 * Fastify 应用组装：鉴权、CORS、路由、错误处理。
 */

import { ASTRA_MODELS, type BridgeRuntime } from "./bridge-runtime.js";
import { randomUUID, timingSafeEqual } from "node:crypto";
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import { dashboardHtml } from "./dashboard.js";
import { ModelStatusService } from "./model-status.js";
import { TRAE_UPSTREAM_MAX_REQUEST_BYTES, type Config } from "./config.js";
import {
  ApiError,
  anthropicTypeForStatus,
  errorBodyFor,
  openaiTypeForStatus,
  protocolForPath,
  statusForUpstreamErrorCategory,
} from "./http/errors.js";
import { buildUpstreamSignal } from "./http/signal.js";
import { estimateInputTokens } from "./routes/count-tokens.js";
import { anthropicToUnified } from "./transform/anthropic-request.js";
import { collectAnthropicMessage } from "./transform/anthropic-nonstream.js";
import { anthropicSseStream } from "./transform/anthropic-stream.js";
import { buildUpstreamBody } from "./transform/upstream-body.js";
import { chatSseStream, collectChatCompletion } from "./transform/chat-stream.js";
import { openaiChatToUnified } from "./transform/chat-request.js";
import { responsesToUnified } from "./transform/responses-request.js";
import { collectResponsesMessage, responsesSseStream } from "./transform/responses-stream.js";
import type { CatalogStore } from "./upstream/catalog.js";
import type { StreamHooks } from "./upstream/engine.js";
import { UpstreamClient, safeText } from "./upstream/client.js";
import { classifyUpstreamError, classifyUpstreamErrorEvent } from "./upstream/events.js";
import { resolveModel } from "./models/resolve.js";
import { buildCodexModelsResponse } from "./models/codex-catalog.js";
import { RequestCorrelation } from "./request-correlation.js";
import type { QuotaCache } from "./upstream/quota.js";
import type { UsageStore } from "./upstream/usage.js";
import { AdmissionPool, admissionEventSource, type AdmissionLease } from "./upstream/admission.js";

export interface AppDeps {
  config: Config;
  bridge?: BridgeRuntime;
  /** 用于 /healthz 暴露凭证剩余有效期 */
  credentials?: {
    expiresInDays(): number | null;
  };
  client: UpstreamClient;
  catalog: CatalogStore;
  quota: QuotaCache;
  usage: UsageStore;
}

/**
 * 上游错误正文 → 给客户端看的 message。
 *
 * 上游返回 `{"error":{"message":"model xxx is rate limited"}}` 时，直接把整段 JSON
 * 当 message 会让客户端拿到一坨转义字符串；WAF 返回整页 HTML 时更糟。
 * 先试着取出真正的错误描述，取不到才回落原文。
 */
export function upstreamErrorMessage(body: string, status: number): string {
  const trimmed = body.trim();
  if (trimmed === "") return `upstream returned ${status}`;

  if (trimmed.startsWith("{")) {
    try {
      const obj = JSON.parse(trimmed) as Record<string, unknown>;
      const err = obj["error"];
      if (typeof err === "string" && err !== "") return err;
      if (typeof err === "object" && err !== null) {
        const m = (err as Record<string, unknown>)["message"];
        if (typeof m === "string" && m !== "") return m;
      }
      const m = obj["message"];
      if (typeof m === "string" && m !== "") return m;
    } catch {
      /* 不是 JSON，走下面的原文回落 */
    }
  }

  const LIMIT = 4096;
  return trimmed.length <= LIMIT
    ? trimmed
    : `${trimmed.slice(0, LIMIT)} […upstream HTTP ${status} body truncated, ${trimmed.length} chars total]`;
}

const SSE_HEADERS = {
  "content-type": "text/event-stream; charset=utf-8",
  "cache-control": "no-cache",
  connection: "keep-alive",
  // nginx 默认会缓冲 SSE，导致首字延迟数秒
  "x-accel-buffering": "no",
  // 流式走 reply.raw.writeHead()，绕过了 Fastify 的 header 机制，
  // onRequest hook 里设的 CORS 头不会生效。少了这行的表现是
  // 「非流式能用、流式被浏览器拦掉」，极难排查。
  "access-control-allow-origin": "*",
} as const;

/** 免鉴权路径 */
const PUBLIC_PATHS = new Set(["/", "/health", "/healthz", "/readyz", "/v1/model-status"]);

function authorized(req: FastifyRequest, expected: string): boolean {
  if (expected === "") return true; // 未配置 API key = 不鉴权（仅 loopback 允许）

  const headers = req.headers;
  const candidates: string[] = [];

  const xApiKey = headers["x-api-key"];
  if (typeof xApiKey === "string") candidates.push(xApiKey.trim());

  const auth = headers["authorization"];
  if (typeof auth === "string") {
    candidates.push(auth.startsWith("Bearer ") ? auth.slice(7).trim() : auth.trim());
  }

  const expectedBuf = Buffer.from(expected, "utf8");
  for (const provided of candidates) {
    const buf = Buffer.from(provided, "utf8");
    // 先比长度短路（长度本身不算敏感信息），再常量时间比较
    if (buf.length === expectedBuf.length && timingSafeEqual(buf, expectedBuf)) return true;
  }
  return false;
}

function isFinitePositiveInteger(value: unknown): boolean {
  return typeof value === "number" && Number.isInteger(value) && Number.isFinite(value) && value > 0;
}

/** messages 缺失、非数组或空数组都是客户端非法请求，不要转发上游换成 502。 */
function requireNonEmptyMessageArray(body: Record<string, unknown>): void {
  const messages = body["messages"];
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new ApiError("messages must be a non-empty array", 400, "invalid_request_error");
  }
}

function requireFinitePositiveIntegerIfPresent(body: Record<string, unknown>, field: string): void {
  if (!Object.prototype.hasOwnProperty.call(body, field)) return;
  if (!isFinitePositiveInteger(body[field])) {
    throw new ApiError(`${field} must be a finite positive integer`, 400, "invalid_request_error");
  }
}

function requireResponsesInput(body: Record<string, unknown>): void {
  if (!Object.prototype.hasOwnProperty.call(body, "input")) {
    throw new ApiError("input is required", 400, "invalid_request_error");
  }
  const input = body["input"];
  if (input === "" || (Array.isArray(input) && input.length === 0)) {
    throw new ApiError("input must be a non-empty string or array", 400, "invalid_request_error");
  }
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const { config, client, catalog, quota, usage, credentials, bridge } = deps;
  const admission = config.admission?.enabled ? new AdmissionPool(config.admission) : undefined;
  const requestCorrelation = new RequestCorrelation(config.apiKey);
  const modelStatus = new ModelStatusService(client, quota);

  const app = Fastify({
    logger: {
      level: config.logLevel,
      redact: {
        paths: [
          "req.headers.authorization",
          'req.headers["x-api-key"]',
          'req.headers["x-upstream-authorization"]',
        ],
        censor: "[REDACTED]",
      },
    },
    bodyLimit: config.maxRequestBytes > 0
      ? config.maxRequestBytes
      : TRAE_UPSTREAM_MAX_REQUEST_BYTES,
    trustProxy: true,
    // 流式响应可能跑几分钟，不能有整体写超时
    connectionTimeout: 0,
    keepAliveTimeout: 120_000,
  });
  app.addHook("preClose", async () => {
    admission?.close();
    modelStatus.close();
  });

  // ---- CORS + 鉴权 ----
  app.addHook("onRequest", async (req, reply) => {
    reply.header("access-control-allow-origin", "*");

    if (req.method === "OPTIONS") {
      const requested = req.headers["access-control-request-headers"];
      reply
        .header("access-control-allow-methods", "GET, POST, OPTIONS")
        .header(
          "access-control-allow-headers",
          typeof requested === "string" && requested !== ""
            ? requested
            : "Authorization, Content-Type, X-Api-Key, anthropic-version, anthropic-beta",
        )
        .header("access-control-max-age", "86400")
        .code(204)
        .send();
      return reply;
    }

    const path = (req.raw.url ?? "").split("?")[0] ?? "";
    if (PUBLIC_PATHS.has(path)) return;

    if (!authorized(req, config.apiKey)) {
      reply.header("www-authenticate", "Bearer");
      const protocol = protocolForPath(path);
      reply.code(401).send(
        errorBodyFor(protocol, 401, "invalid API key", undefined, "invalid_api_key"),
      );
      return reply;
    }
    return;
  });

  bridge?.install(app);

  // ---- 统一错误处理 ----
  app.setErrorHandler((rawError, req, reply) => {
    const error = rawError as Error & { statusCode?: number };
    const apiErr = error as Partial<ApiError> & Error;
    const status = typeof apiErr.status === "number" ? apiErr.status : (error.statusCode ?? 500);
    const protocol = protocolForPath(req.raw.url ?? "");
    const message = error.message || "internal error";

    if (status >= 500) req.log.error({ err: error }, "request failed");
    else req.log.warn({ msg: message }, "request rejected");

    if (reply.sent || reply.raw.headersSent) return;
    if (status === 429 && apiErr.code === "rate_limit_exceeded") reply.header("retry-after", "60");
    reply.code(status).send(errorBodyFor(protocol, status, message, apiErr.errorType, apiErr.code));
  });

  app.get("/", async (_req, reply) => reply
    .header("cache-control", "no-store")
    .header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src https://lf-cdn.trae.com.cn https://openrouter.ai data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    .header("referrer-policy", "no-referrer")
    .header("x-content-type-options", "nosniff")
    .type("text/html; charset=utf-8")
    .send(dashboardHtml));

  app.get("/v1/model-status", async (_req, reply) => {
    reply.header("cache-control", "no-store");
    return modelStatus.get();
  });

  // ---- 健康检查 ----
  for (const p of ["/healthz", "/health", "/readyz"]) {
    app.get(p, async () => {
      if (bridge) return bridge.health(catalog.list(), {
        queue_timeout: (config.admission?.waitTimeoutMs ?? 0) / 1000,
        max_requests: config.admission?.maxRequests ?? 8,
        stream_idle_timeout: config.streamIdleTimeoutMs / 1000,
        upstream_header_timeout: config.upstreamHeaderTimeoutMs / 1000,
      });
      const days = credentials?.expiresInDays() ?? null;
      return {
        ok: true,
        models: catalog.size,
        generation_mode: "stateless",
        // 凭证快过期时能一眼看出来，不用等到满屏 401 才排查
        ...(days === null ? {} : { token_expires_in_days: Math.round(days * 10) / 10 }),
      };
    });
  }

  // ---- 模型 ----
  const modelPayload = (id: string): Record<string, unknown> | null => {
    const m = catalog.lookup(id);
    if (!m) return null;
    const out: Record<string, unknown> = {
      id: m.id,
      object: "model",
      created: 0,
      owned_by: "bytedance-trae",
      usage_limited: m.usageLimited,
    };
    if (m.contextWindow > 0) out["context_window"] = m.contextWindow;
    if (m.repoRestricted) out["repo_restricted"] = true;
    if (m.superModel) out["super_model"] = true;
    if (m.feeLevel > 0) out["fee_level"] = m.feeLevel;
    if (m.manualUsage > 0) out["manual_usage"] = m.manualUsage;
    if (m.reasoning) {
      const r: Record<string, unknown> = {};
      if (m.reasoning.supportedEfforts.length > 0) r["supported_efforts"] = m.reasoning.supportedEfforts;
      if (m.reasoning.defaultEffort !== "") r["default_effort"] = m.reasoning.defaultEffort;
      if (Object.keys(r).length > 0) out["reasoning"] = r;
    }
    if (m.usageLimited) {
      const q = quota.perModelSnapshot(m.model);
      if (q) out["quota"] = q;
    }
    return out;
  };

  for (const p of ["/v1/models", "/models"]) {
    app.get(p, async (req) => {
      const query = req.query as Record<string, unknown>;
      if (Object.hasOwn(query, "client_version")) {
        return buildCodexModelsResponse(catalog.list());
      }
      return {
        object: "list",
        data: catalog.list().map((m) => modelPayload(m.id)).filter(Boolean),
      };
    });
  }

  for (const p of ["/v1/models/*", "/models/*"]) {
    app.get(p, async (req, reply) => {
      const path = (req.raw.url ?? "").split("?")[0] ?? "";
      const id = decodeURIComponent(
        path.replace(/^\/v1\/models\//, "").replace(/^\/models\//, ""),
      );
      const payload = id === "" ? null : modelPayload(id);
      if (!payload) {
        return reply.code(404).send(
          errorBodyFor("openai", 404, `model ${JSON.stringify(id)} not found`, undefined, "model_not_found"),
        );
      }
      return payload;
    });
  }

  // ---- 用量 ----
  for (const p of ["/v1/usage", "/usage"]) {
    app.get(p, async () => {
      const snapshot = await quota.get().catch(() => null);
      return {
        object: "usage",
        observed: usage.snapshot(),
        ...(snapshot ? { quota: snapshot.quota, models: snapshot.models } : {}),
      };
    });
  }

  // 旧版暴露过跨请求排队/会话状态。无状态模式不再维护全局生成请求表，
  // 暂时保留 410 响应，给自动化调用方一个明确的迁移信号。
  for (const p of ["/v1/queue", "/queue"]) {
    app.get(p, async (_req, reply) => reply.code(410).send({
      object: "trae.queue_state",
      stateless: true,
      message: "cross-request queue state is not available in stateless mode",
    }));
  }

  function prepareUpstream(
    req: FastifyRequest,
    reply: FastifyReply,
    unified: ReturnType<typeof anthropicToUnified>["request"],
    requestedModel: string,
    downstreamBody: Record<string, unknown>,
  ) {
    // Anthropic 的 context-1m beta 会让统一层的 model 与响应回显用的
    // requestedModel 不同；路由必须尊重统一层已经归一化过的 model。
    const routingModel = typeof unified.model === "string" && unified.model.trim() !== ""
      ? unified.model
      : requestedModel;
    const resolved = resolveModel(catalog, routingModel, config.defaultModel);
    if (config.logRequests) {
      req.log.info(
        {
          requested_model: requestedModel,
          routing_model: routingModel,
          upstream_config: resolved.config,
          upstream_model: resolved.model,
        },
        "request routed",
      );
    }
    const downstream = buildUpstreamSignal(req, reply);
    const signal = admission ? AbortSignal.any([downstream, admission.shutdown.signal]) : downstream;
    const requestId = randomUUID();

    const body = buildUpstreamBody(unified, {
      config: resolved.config,
      model: resolved.model,
    });
    const correlation = requestCorrelation.summarizeRequest(
      req.headers,
      downstreamBody,
      body,
      requestId,
    );
    req.log.info(
      {
        model: resolved.model,
        ...correlation,
      },
      "upstream request context",
    );
    usage.beginRequest(requestId, resolved.model);
    bridge?.event(req.id, "start");

    let finalized = false;
    const finalize = (): void => {
      if (finalized) return;
      finalized = true;
      usage.finishRequest(requestId);
    };

    /** 上游流生命周期只影响当前请求的日志与观测，不建立任何跨请求关联。 */
    const hooks: StreamHooks = {
      onActivity: () => bridge?.event(req.id, "activity"),
      onQueue: (q) => {
        bridge?.event(req.id, "queue");
        req.log.info(
          {
            model: resolved.model,
            position: q.position ?? null,
            ...requestCorrelation.summarizeQueue(correlation, q),
          },
          "upstream queue wait",
        );
      },
      onUsage: (u) => { usage.updateRequestUsage(requestId, u); bridge?.event(req.id, "usage", u); },
      onErrorEvent: (raw) => {
        bridge?.event(req.id, "error");
        // 200 流内的 error 事件是「额度耗尽」最常见的形态
        const category = classifyUpstreamError(0, raw);
        usage.recordError(resolved.model, category);
        req.log.warn(
          {
            model: resolved.model,
            category,
            code: classifyUpstreamErrorEvent(raw).code,
            bridge_request_hmac: correlation.bridge_request_hmac,
            context_hmac: correlation.context_hmac,
          },
          "upstream stream error event",
        );
      },
      onFinish: (outcome) => {
        bridge?.event(req.id, "finish", outcome);
        req.log.info(
          {
            model: resolved.model,
            outcome,
            bridge_request_hmac: correlation.bridge_request_hmac,
            context_hmac: correlation.context_hmac,
          },
          "upstream request finished",
        );
        finalize();
      },
    };
    return { body, resolved, signal, hooks, finalize, correlation };
  }

  /** 三个生成端点共用的上游调用 */
  async function callUpstream(
    req: FastifyRequest,
    reply: FastifyReply,
    unified: ReturnType<typeof anthropicToUnified>["request"],
    requestedModel: string,
    downstreamBody: Record<string, unknown>,
  ): Promise<
    { res: Response; signal: AbortSignal; hooks: StreamHooks } | { error: ApiError }
  > {
    const { body, resolved, signal, hooks, finalize, correlation } = prepareUpstream(
      req,
      reply,
      unified,
      requestedModel,
      downstreamBody,
    );
    const attempt = requestCorrelation.summarizeAttempt(correlation, body);
    req.log.info({ model: resolved.model, ...attempt }, "upstream attempt");

    let res: Response;
    try {
      res = await client.chat(body, signal);
    } catch (e) {
      const msg = (e as Error).message;
      if (signal.aborted) {
        finalize();
        return { error: new ApiError("client disconnected", 499, "request_canceled") };
      }
      finalize();
      return { error: new ApiError(`upstream connect: ${msg}`, 502, "api_error") };
    }
    req.log.info(
      { model: resolved.model, status: res.status, ...attempt },
      "upstream response headers",
    );

    if (!res.ok) {
      finalize();
      const text = await safeText(res, 4096);
      const event = classifyUpstreamErrorEvent(text);
      const category = classifyUpstreamError(res.status, text);
      usage.recordError(resolved.model, category);
      const status = event.terminal
        ? statusForUpstreamErrorCategory(event.category, res.status)
        : (res.status >= 400 && res.status < 600 ? res.status : 502);
      const protocol = protocolForPath(req.raw.url ?? "");
      const errorType = protocol === "anthropic"
        ? anthropicTypeForStatus(status)
        : openaiTypeForStatus(status);
      const message = event.terminal && event.message !== ""
        ? event.message
        : upstreamErrorMessage(text, res.status);
      return { error: new ApiError(message, status, errorType) };
    }

    if (!res.body) {
      finalize();
      return { error: new ApiError("upstream returned an empty body", 502, "api_error") };
    }

    return { res, signal, hooks };
  }

  // ---- Anthropic Messages ----
  for (const p of ["/v1/messages", "/messages"]) {
    app.post(p, async (req, reply) => {
      const body = req.body;
      if (typeof body !== "object" || body === null) {
        throw new ApiError("request body must be a JSON object", 400, "invalid_request_error");
      }
      const payload = body as Record<string, unknown>;
      requireNonEmptyMessageArray(payload);
      requireFinitePositiveIntegerIfPresent(payload, "max_tokens");
      const parsed = anthropicToUnified(
        payload,
        config.defaultModel,
        req.headers["anthropic-beta"],
      );

      const call = await callUpstream(req, reply, parsed.request, parsed.requestedModel, payload);
      if ("error" in call) throw call.error;

      if (parsed.stream) {
        reply.raw.writeHead(200, SSE_HEADERS);
        const stream = anthropicSseStream({
          upstreamBody: call.res.body!,
          requestedModel: parsed.requestedModel,
          stopSequences: parsed.stopSequences,
          idleMs: config.streamIdleTimeoutMs,
          signal: call.signal,
          hooks: call.hooks,
        });
        await pipeToRaw(stream, reply);
        return reply;
      }

      const result = await collectAnthropicMessage(
        call.res.body!,
        parsed.requestedModel,
        config.streamIdleTimeoutMs,
        call.signal,
        call.hooks,
      );
      if (!result.ok) {
        throw new ApiError(result.message, result.status, result.errorType);
      }
      return result.body;
    });
  }

  for (const p of ["/v1/messages/count_tokens", "/messages/count_tokens"]) {
    app.post(p, async (req) => {
      const body = req.body;
      if (typeof body !== "object" || body === null) {
        throw new ApiError("request body must be a JSON object", 400, "invalid_request_error");
      }
      return { input_tokens: estimateInputTokens(body as Record<string, unknown>) };
    });
  }

  // ---- OpenAI Chat Completions ----
  for (const p of ["/v1/chat/completions", "/chat/completions"]) {
    app.post(p, async (req, reply) => {
      const body = req.body;
      if (typeof body !== "object" || body === null) {
        throw new ApiError("request body must be a JSON object", 400, "invalid_request_error");
      }
      const payload = body as Record<string, unknown>;
      requireNonEmptyMessageArray(payload);
      requireFinitePositiveIntegerIfPresent(payload, "max_tokens");
      requireFinitePositiveIntegerIfPresent(payload, "max_completion_tokens");
      const parsed = openaiChatToUnified(payload, config.defaultModel);

      const call = await callUpstream(req, reply, parsed.request, parsed.requestedModel, payload);
      if ("error" in call) throw call.error;

      if (parsed.stream) {
        reply.raw.writeHead(200, SSE_HEADERS);
        const stream = chatSseStream({
          upstreamBody: call.res.body!,
          requestedModel: parsed.requestedModel,
          includeUsage: parsed.includeUsage,
          idleMs: config.streamIdleTimeoutMs,
          signal: call.signal,
          hooks: call.hooks,
          structuredOutput: parsed.structuredOutput,
        });
        await pipeToRaw(stream, reply);
        return reply;
      }

      const result = await collectChatCompletion(
        call.res.body!,
        parsed.requestedModel,
        config.streamIdleTimeoutMs,
        call.signal,
        call.hooks,
        parsed.structuredOutput,
      );
      if (!result.ok) throw new ApiError(result.message, result.status, result.errorType);
      return result.body;
    });
  }

  // ---- OpenAI Responses ----
  for (const p of ["/v1/responses", "/responses"]) {
    app.post(p, async (req, reply) => {
      const body = req.body;
      if (typeof body !== "object" || body === null) {
        throw new ApiError("request body must be a JSON object", 400, "invalid_request_error");
      }
      const payload = body as Record<string, unknown>;
      if (bridge) {
        const model = payload["model"] ?? config.defaultModel;
        if (typeof model !== "string" || !ASTRA_MODELS.has(model) || !catalog.lookup(model)) {
          throw new ApiError("Requested Astra model is unavailable", 400, "invalid_request_error", "model_not_found");
        }
        const effort = (payload["reasoning"] as Record<string, unknown> | undefined)?.["effort"];
        if (effort !== undefined && !catalog.lookup(model)?.reasoning?.supportedEfforts.includes(String(effort))) {
          throw new ApiError("Unsupported reasoning effort", 400, "invalid_request_error");
        }
        if (payload["previous_response_id"]) throw new ApiError("Send full input history; response IDs are not stored", 400, "invalid_request_error");
      }
      requireResponsesInput(payload);
      requireFinitePositiveIntegerIfPresent(payload, "max_output_tokens");
      const parsed = responsesToUnified(payload, config.defaultModel);
      if (parsed.empty) {
        throw new ApiError("empty input", 400, "invalid_request_error");
      }

      bridge?.begin(req, parsed.requestedModel, parsed.stream);
      try {
      if (parsed.stream && admission) {
        const prepared = prepareUpstream(
          req,
          reply,
          parsed.request,
          parsed.requestedModel,
          payload,
        );
        let lease: AdmissionLease | undefined;
        try {
          const principal = await client.principalScope();
          prepared.signal.throwIfAborted();
          const key = JSON.stringify([principal, prepared.resolved.config, prepared.resolved.model]);
          lease = admission.reserve(key, Buffer.byteLength(JSON.stringify(prepared.body)));
          bridge?.event(req.id, "queue");
          reply.raw.writeHead(200, SSE_HEADERS);
          const stream = responsesSseStream({
            eventSource: admissionEventSource({
              config: admission.config,
              pool: admission,
              lease,
              idleMs: config.streamIdleTimeoutMs,
              open: async (signal) => {
                const attemptBody = {
                  ...prepared.body,
                  session_id: randomUUID(),
                  conversation_id: randomUUID(),
                };
                const attempt = requestCorrelation.summarizeAttempt(
                  prepared.correlation,
                  attemptBody,
                );
                req.log.info(
                  { model: prepared.resolved.model, ...attempt },
                  "upstream attempt",
                );
                const response = await client.chat(attemptBody, signal, principal);
                req.log.info(
                  { model: prepared.resolved.model, status: response.status, ...attempt },
                  "upstream response headers",
                );
                return response;
              },
              log: (phase, fields) => req.log.info({
                model: prepared.resolved.model,
                bridge_request_hmac: prepared.correlation.bridge_request_hmac,
                context_hmac: prepared.correlation.context_hmac,
                ...fields,
              }, phase),
            }),
            requestedModel: parsed.requestedModel,
            toolMeta: parsed.toolMeta,
            idleMs: config.streamIdleTimeoutMs,
            signal: prepared.signal,
            hooks: prepared.hooks,
            structuredOutput: parsed.structuredOutput,
            deferLifecycle: config.responsesDeferLifecycle,
          });
          await pipeToRaw(stream, reply);
          return reply;
        } finally {
          lease?.release();
          prepared.finalize();
        }
      }

      const call = await callUpstream(req, reply, parsed.request, parsed.requestedModel, payload);
      if ("error" in call) throw call.error;

      if (parsed.stream) {
        reply.raw.writeHead(200, SSE_HEADERS);
        const stream = responsesSseStream({
          upstreamBody: call.res.body!,
          requestedModel: parsed.requestedModel,
          toolMeta: parsed.toolMeta,
          idleMs: config.streamIdleTimeoutMs,
          signal: call.signal,
          hooks: call.hooks,
          structuredOutput: parsed.structuredOutput,
          deferLifecycle: config.responsesDeferLifecycle,
        });
        await pipeToRaw(stream, reply);
        return reply;
      }

      const result = await collectResponsesMessage(
        call.res.body!,
        parsed.requestedModel,
        parsed.toolMeta,
        config.streamIdleTimeoutMs,
        call.signal,
        call.hooks,
        parsed.structuredOutput,
      );
      if (!result.ok) throw new ApiError(result.message, result.status, result.errorType);
      return result.body;
      } finally { bridge?.end(req); }
    });
  }

  return app;
}

/**
 * 把 Web ReadableStream 写进 Node 的 raw response。
 *
 * 不用 `reply.send(stream)` 是因为我们已经 `writeHead` 过了（要精确控制 SSE 头）。
 */
export async function pipeToRaw(stream: ReadableStream<Uint8Array>, reply: FastifyReply): Promise<void> {
  const reader = stream.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (reply.raw.destroyed) break;
      const ok = reply.raw.write(value);
      if (!ok) {
        // 背压：等 drain 再继续，避免无限缓冲。
        //
        // 必须同时等 close/error —— 客户端在 write 返回 false 之后、drain 之前断开时
        // socket 被 destroy，`drain` 永远不会来，这个 Promise 会永久挂起，
        // 连带 finally 里的 reader.cancel() 和 raw.end() 都执行不到。
        await new Promise<void>((resolve) => {
          const done = (): void => {
            reply.raw.off("drain", done);
            reply.raw.off("close", done);
            reply.raw.off("error", done);
            resolve();
          };
          reply.raw.once("drain", done);
          reply.raw.once("close", done);
          reply.raw.once("error", done);
        });
      }
    }
  } catch {
    /* 客户端断开 */
  } finally {
    try { await reader.cancel(); } catch { /* ignore */ }
    if (!reply.raw.writableEnded) reply.raw.end();
  }
}
