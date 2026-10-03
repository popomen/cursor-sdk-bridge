/**
 * Trae 上游 HTTP 客户端。
 *
 * 三个端点，同 host 同鉴权方案：
 *   POST /api/ide/v2/llm_raw_chat            生成（唯一），SSE
 *   POST /api/ide/v1/get_detail_param        模型目录，JSON
 *   POST /trae/api/v1/pay/ide_user_ent_usage 账号额度，JSON
 *
 * 超时纪律（这条踩过坑）：**绝不设整体请求超时**。对流式响应它是绝对死线，
 * 会腰斩正在正常产出的 SSE。取而代之：
 *   - header timeout：只限制「发出请求 → 响应头到达」这一段。fetch 的 promise
 *     恰好在响应头到达时 resolve，所以拿到 Response 后撤销定时器即可，body 流不受限。
 *   - SSE idle timeout：由 readUpstreamSSE 负责，限制两次数据之间的最大间隔。
 */

import {
  tokenPrincipalScope,
  type CredentialProvider,
} from "./credentials.js";
import { createHash } from "node:crypto";
import { buildTraePpeHeaders } from "./ppe.js";

export const TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8";
export const CHAT_PATH = "/api/ide/v2/llm_raw_chat";
export const CATALOG_PATH = "/api/ide/v1/get_detail_param";
export const QUOTA_PATH = "/trae/api/v1/pay/ide_user_ent_usage";

/** `x-ide-version-code` 是当天日期，每次请求现算（本地时区，对齐 Go 的 time.Now()） */
export function ideVersionCode(now = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}${m}${d}`;
}

export class UpstreamHttpError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`upstream returned ${status}`);
    this.name = "UpstreamHttpError";
  }
}

export class CredentialPrincipalChangedError extends Error {
  constructor() {
    super("credential principal changed while request was in flight; retry the request");
    this.name = "CredentialPrincipalChangedError";
  }
}

export interface UpstreamClientOptions {
  baseUrl: string;
  catalogBaseUrl: string;
  /** 与 TraeX 的 TRAE_PPE_ENV 同义；启用后给所有 Trae 上游请求加 PPE headers */
  traePpeEnv?: string;
  credentials: CredentialProvider;
  headerTimeoutMs: number;
  dumpBody?: boolean;
  logger?: { info(...a: unknown[]): void; warn(...a: unknown[]): void; error(...a: unknown[]): void };
}

export class UpstreamClient {
  private readonly traePpeHeaders: Record<string, string>;

  constructor(private readonly opts: UpstreamClientOptions) {
    this.traePpeHeaders = buildTraePpeHeaders(opts.traePpeEnv);
  }

  /** Opaque credentials are pinned to their exact value; never share an empty scope. */
  private principalFor(token: string): string {
    return tokenPrincipalScope(token) || createHash("sha256").update(token).digest("base64url");
  }

  async principalScope(): Promise<string> {
    return this.principalFor(await this.opts.credentials.token());
  }

  /**
   * 生成接口。返回原始 Response（body 是 SSE 流，由调用方消费）。
   * 401 时刷新 token 重试一次。
   */
  async chat(body: unknown, signal?: AbortSignal, expectedPrincipal?: string): Promise<Response> {
    if (this.opts.dumpBody) {
      this.opts.logger?.info("[upstream] llm_raw_chat body:", JSON.stringify(body));
    }
    const url = this.opts.baseUrl + CHAT_PATH;
    const payload = JSON.stringify(body);

    for (let attempt = 0; attempt < 2; attempt++) {
      signal?.throwIfAborted();
      const token = await this.opts.credentials.token();
      signal?.throwIfAborted();
      if (expectedPrincipal !== undefined && this.principalFor(token) !== expectedPrincipal) {
        throw new CredentialPrincipalChangedError();
      }
      const res = await this.fetchWithHeaderTimeout(
        url,
        {
          method: "POST",
          headers: {
            Authorization: `Cloud-CLI-JWT ${token}`, // 注意不是 Bearer，Bearer 会 401
            "Content-Type": "application/json",
            Accept: "text/event-stream",
            "x-app-id": TRAE_APP_ID,
            "x-ide-function": "traecli_next",
            "x-ide-version-code": ideVersionCode(),
            originator: "codex_exec",
            ...this.traePpeHeaders,
          },
          body: payload,
        },
        signal,
      );

      if (res.status === 401 && attempt === 0) {
        // 排空并关闭 body 再重试，避免连接泄漏
        await drainAndClose(res);
        const refreshed = await this.opts.credentials.refresh(token);
        if (tokenPrincipalScope(refreshed) !== tokenPrincipalScope(token)) {
          throw new CredentialPrincipalChangedError();
        }
        if (expectedPrincipal !== undefined && this.principalFor(refreshed) !== expectedPrincipal) {
          throw new CredentialPrincipalChangedError();
        }
        continue;
      }
      return res;
    }
    throw new Error("unreachable: chat retry loop");
  }

  /** catalog / quota 用的 JSON POST。与 chat 的 header 有三处差异，别照抄。 */
  private async authenticatedJsonPost(url: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
    let lastErr: Error | null = null;

    for (let attempt = 0; attempt < 2; attempt++) {
      const token = await this.opts.credentials.token();
      const res = await this.fetchWithHeaderTimeout(
        url,
        {
          method: "POST",
          headers: {
            Authorization: `Cloud-CLI-JWT ${token}`,
            "Content-Type": "application/json",
            // 无 Accept: text/event-stream（响应是普通 JSON）
            // 无 x-ide-function（该信息由 body 的 "function" 字段承载）
            "x-app-id": TRAE_APP_ID,
            "x-ide-version-code": ideVersionCode(),
            originator: "codex_cli_rs", // 与 chat 的 codex_exec 不同
            ...this.traePpeHeaders,
          },
          body: JSON.stringify(body),
        },
        signal,
      );

      if (res.status === 401 && attempt === 0) {
        await drainAndClose(res);
        const refreshed = await this.opts.credentials.refresh(token);
        if (tokenPrincipalScope(refreshed) !== tokenPrincipalScope(token)) {
          throw new CredentialPrincipalChangedError();
        }
        continue;
      }

      if (!res.ok) {
        const text = (await safeText(res, 2048)).trim();
        lastErr = new UpstreamHttpError(res.status, text);
        throw lastErr;
      }
      return await res.json();
    }
    throw lastErr ?? new Error("unreachable: json post retry loop");
  }

  async getDetailParam(signal?: AbortSignal, fn = "traecli_next"): Promise<unknown> {
    return this.authenticatedJsonPost(
      this.opts.catalogBaseUrl + CATALOG_PATH,
      { function: fn, need_prompt: false, poly_prompt: false },
      signal,
    );
  }

  async getAccountUsage(signal?: AbortSignal): Promise<unknown> {
    // quota 永远跟 chat 走同一个 base，不跟 catalog
    return this.authenticatedJsonPost(
      this.opts.baseUrl + QUOTA_PATH,
      { function: "traecli_next" },
      signal,
    );
  }

  /**
   * 只对「响应头到达」设超时；拿到 Response 后立刻撤销定时器，让 body 流无限期地流。
   * 下游 signal 全程有效，用于把客户端断连传播到上游连接。
   */
  private async fetchWithHeaderTimeout(
    url: string,
    init: RequestInit,
    downstream?: AbortSignal,
  ): Promise<Response> {
    const ac = new AbortController();
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      ac.abort(new Error("upstream response header timeout"));
    }, this.opts.headerTimeoutMs);
    timer.unref?.();

    const onDownstreamAbort = () => ac.abort(new Error("downstream canceled"));
    if (downstream) {
      if (downstream.aborted) ac.abort(new Error("downstream canceled"));
      else downstream.addEventListener("abort", onDownstreamAbort, { once: true });
    }

    try {
      const res = await fetch(url, { ...init, signal: ac.signal, redirect: "manual" });
      // 头到了 —— 撤销 header 超时，body 流不再受它约束
      clearTimeout(timer);
      return res;
    } catch (e) {
      clearTimeout(timer);
      if (timedOut) {
        throw new Error(`upstream response header timeout after ${this.opts.headerTimeoutMs}ms`);
      }
      throw e;
    } finally {
      // 注意：不能在这里 removeEventListener 之后就丢掉 ac —— body 流仍需要
      // 下游 abort 能传播过去，所以监听器保留到请求自然结束。
      if (downstream && (timedOut || downstream.aborted)) {
        downstream.removeEventListener("abort", onDownstreamAbort);
      }
    }
  }
}

async function drainAndClose(res: Response): Promise<void> {
  try {
    // 最多读 64 KiB 再丢弃，让连接可以复用
    await res.body?.cancel();
  } catch {
    /* ignore */
  }
}

export async function safeText(res: Response, limit: number): Promise<string> {
  try {
    const text = await res.text();
    return text.length > limit ? text.slice(0, limit) : text;
  } catch {
    return "";
  }
}
