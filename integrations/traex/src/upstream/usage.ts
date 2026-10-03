/**
 * 本进程观测到的用量与错误。
 *
 * 与 `/v1/usage` 里的 `quota` 不同：那个是上游权威额度（有几分钟延迟），
 * 这个是本进程实时看到的事实。两者叠加才能得到准确的"这个模型现在能不能用"。
 */

import type { UpstreamErrorCategory } from "./events.js";
import type { UpstreamUsage } from "../types/unified.js";

export interface ObservedModelUsage {
  model: string;
  requests: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_write_input_tokens: number;
  /** cache_read / (cache_read + cache_write)，没有缓存样本时为 0 */
  cache_hit_rate: number;
  last_used?: number;
  limited: boolean;
  limit_kind?: string;
  limited_at?: number;
}

interface Entry {
  requests: number;
  errors: number;
  input: number;
  output: number;
  total: number;
  cacheRead: number;
  cacheWrite: number;
  lastUsed: number;
  limited: boolean;
  limitKind: string;
  limitedAt: number;
}

interface RequestUsageSample {
  model: string;
  usage: UpstreamUsage | null;
}

export class UsageStore {
  private readonly byModel = new Map<string, Entry>();
  private readonly requests = new Map<string, RequestUsageSample>();

  private entry(model: string): Entry {
    let e = this.byModel.get(model);
    if (!e) {
      e = {
        requests: 0,
        errors: 0,
        input: 0,
        output: 0,
        total: 0,
        cacheRead: 0,
        cacheWrite: 0,
        lastUsed: 0,
        limited: false,
        limitKind: "",
        limitedAt: 0,
      };
      this.byModel.set(model, e);
    }
    return e;
  }

  /** 一个真实生成请求只计数一次，与上游发了几个 usage 事件无关。 */
  beginRequest(requestId: string, model: string): void {
    if (requestId === "" || model === "" || this.requests.has(requestId)) return;
    const e = this.entry(model);
    e.requests += 1;
    e.lastUsed = Math.floor(Date.now() / 1000);
    this.requests.set(requestId, { model, usage: null });
  }

  /**
   * usage 事件是当前请求的累计快照。多帧时只把“新快照 - 旧快照”的差值计入总量，
   * 避免把 10 → 20 误算成 30。
   */
  updateRequestUsage(requestId: string, u: UpstreamUsage): void {
    const request = this.requests.get(requestId);
    if (!request) return;
    const e = this.entry(request.model);
    const previous = request.usage;
    e.input += u.prompt_tokens - (previous?.prompt_tokens ?? 0);
    e.output += u.completion_tokens - (previous?.completion_tokens ?? 0);
    e.total += u.total_tokens - (previous?.total_tokens ?? 0);
    e.cacheRead += u.cache_read_input_tokens - (previous?.cache_read_input_tokens ?? 0);
    e.cacheWrite += u.cache_write_input_tokens - (previous?.cache_write_input_tokens ?? 0);
    e.lastUsed = Math.floor(Date.now() / 1000);
    request.usage = u;
    // 成功一次就清除限流标记
    e.limited = false;
    e.limitKind = "";
    e.limitedAt = 0;
  }

  finishRequest(requestId: string): void {
    this.requests.delete(requestId);
  }

  /**
   * 记录一次上游错误。
   *
   * **只有 quota 和 rate_limit 会把模型标记为 limited** —— auth 是运维问题、
   * invalid_request 是我们自己的请求形状 bug，都不代表模型被限流。
   */
  recordError(model: string, category: UpstreamErrorCategory): void {
    if (model === "") return;
    const e = this.entry(model);
    e.errors += 1;
    if (category === "quota" || category === "rate_limit") {
      e.limited = true;
      e.limitKind = category;
      e.limitedAt = Math.floor(Date.now() / 1000);
    }
  }

  /** 该模型当前是否因额度耗尽被实时观测到限流 */
  isQuotaLimited(model: string): boolean {
    const e = this.byModel.get(model);
    return e ? e.limited && e.limitKind === "quota" : false;
  }

  snapshot(): ObservedModelUsage[] {
    return [...this.byModel.entries()]
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([model, e]) => {
        const cacheTotal = e.cacheRead + e.cacheWrite;
        return {
          model,
          requests: e.requests,
          errors: e.errors,
          input_tokens: e.input,
          output_tokens: e.output,
          total_tokens: e.total,
          cache_read_input_tokens: e.cacheRead,
          cache_write_input_tokens: e.cacheWrite,
          cache_hit_rate: cacheTotal === 0 ? 0 : e.cacheRead / cacheTotal,
          ...(e.lastUsed > 0 ? { last_used: e.lastUsed } : {}),
          limited: e.limited,
          ...(e.limitKind !== "" ? { limit_kind: e.limitKind } : {}),
          ...(e.limitedAt > 0 ? { limited_at: e.limitedAt } : {}),
        };
      });
  }
}
