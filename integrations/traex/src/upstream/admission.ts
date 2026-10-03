import type { AdmissionConfig } from "../config.js";
import { ApiError, openaiTypeForStatus } from "../http/errors.js";
import { CredentialPrincipalChangedError } from "./client.js";
import { classifyOutcome, runUpstreamStream, type EngineEvent, type StreamHooks, type StreamState } from "./engine.js";
import { classifyUpstreamErrorEvent, UPSTREAM_ERROR_CODE_QUEUE_FULL } from "./events.js";

const HEARTBEAT_MS = 10_000;
const MAX_ERROR_BYTES = 64 * 1024;

export const admissionLimitError = (message: string): ApiError =>
  new ApiError(`${message}. Please try again in 60 seconds.`, 429, "rate_limit_error", "rate_limit_exceeded");

export function admissionDelay(config: AdmissionConfig, rejection: number, random = Math.random()): number {
  const base = Math.min(config.maxDelayMs, config.initialDelayMs * 2 ** Math.min(rejection - 1, 30));
  return Math.min(config.maxDelayMs, Math.max(1, Math.round(base * (0.8 + random * 0.4))));
}

export function retryAfterMs(value: string | null, now = Date.now()): number {
  if (!value) return 0;
  if (/^\d+(?:\.\d+)?$/.test(value.trim())) {
    const ms = Number(value) * 1000;
    return Number.isFinite(ms) ? ms : 0;
  }
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - now) : 0;
}

interface Waiter {
  lease: AdmissionLease;
  resolve: () => void;
  reject: (error: unknown) => void;
  cleanup: () => void;
}

interface Group {
  members: number;
  nextAt: number;
  active?: AdmissionLease;
  queue: Waiter[];
  timer?: ReturnType<typeof setTimeout>;
}

/** Stores only scheduling metadata. Payloads and conversations stay in their own request. */
export class AdmissionPool {
  readonly shutdown = new AbortController();
  private groups = new Map<string, Group>();
  private count = 0;
  private bytes = 0;

  constructor(readonly config: AdmissionConfig) {}

  snapshot(): { requests: number; bytes: number; groups: number } {
    return { requests: this.count, bytes: this.bytes, groups: this.groups.size };
  }

  reserve(key: string, bytes: number): AdmissionLease {
    this.shutdown.signal.throwIfAborted();
    if (this.count >= this.config.maxRequests || bytes > this.config.maxBytes - this.bytes) {
      throw admissionLimitError("Local admission capacity is full; request was not submitted upstream");
    }
    let group = this.groups.get(key);
    if (!group) {
      group = { members: 0, nextAt: 0, queue: [] };
      this.groups.set(key, group);
    }
    group.members++;
    this.count++;
    this.bytes += bytes;
    return new AdmissionLease(this, key, group, bytes);
  }

  acquire(lease: AdmissionLease, group: Group, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    return new Promise((resolve, reject) => {
      let settled = false;
      let waiter!: Waiter;
      const cleanup = (): void => signal.removeEventListener("abort", onAbort);
      const settleResolve = (): void => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve();
      };
      const settleReject = (error: unknown): void => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const onAbort = (): void => {
        if (settled) return;
        const index = group.queue.indexOf(waiter);
        if (index >= 0) group.queue.splice(index, 1);
        settleReject(signal.reason);
        this.pump(lease.key, group);
      };
      waiter = {
        lease,
        resolve: settleResolve,
        reject: settleReject,
        cleanup,
      };
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) {
        onAbort();
        return;
      }
      group.queue.push(waiter);
      // Defensive second check for wrapped AbortSignal implementations with re-entrant hooks.
      if (signal.aborted) {
        onAbort();
        return;
      }
      this.pump(lease.key, group);
    });
  }

  finishAttempt(lease: AdmissionLease, group: Group, delayMs: number): void {
    if (group.active === lease) group.active = undefined;
    group.nextAt = Math.max(group.nextAt, performance.now() + delayMs);
    this.pump(lease.key, group);
  }

  release(lease: AdmissionLease, group: Group, bytes: number): void {
    const pending = group.queue.filter((waiter) => waiter.lease === lease);
    group.queue = group.queue.filter((waiter) => waiter.lease !== lease);
    for (const waiter of pending) {
      waiter.cleanup();
      waiter.reject(new Error("admission reservation released"));
    }
    if (group.active === lease) group.active = undefined;
    group.members--;
    this.count--;
    this.bytes -= bytes;
    this.pump(lease.key, group);
  }

  private pump(key: string, group: Group): void {
    if (group.timer) clearTimeout(group.timer);
    group.timer = undefined;
    if (group.active) return;
    // Empty groups are removed, so arbitrary model names cannot accumulate state.
    if (group.members === 0) {
      this.groups.delete(key);
      return;
    }
    if (group.queue.length === 0) return;
    const delay = group.nextAt - performance.now();
    if (delay > 0) {
      group.timer = setTimeout(() => this.pump(key, group), Math.min(delay, 2_147_483_647));
      return;
    }
    const waiter = group.queue.shift()!;
    waiter.cleanup();
    group.active = waiter.lease;
    waiter.resolve();
  }

  close(): void {
    this.shutdown.abort(new Error("bridge is shutting down"));
    for (const group of this.groups.values()) {
      if (group.timer) clearTimeout(group.timer);
    }
  }
}

export class AdmissionLease {
  private released = false;
  constructor(
    private pool: AdmissionPool,
    readonly key: string,
    private group: Group,
    private bytes: number,
  ) {}

  acquire(signal: AbortSignal): Promise<void> {
    if (this.released) throw new Error("admission reservation already released");
    return this.pool.acquire(this, this.group, signal);
  }

  retryInMs(): number { return Math.max(0, Math.ceil(this.group.nextAt - performance.now())); }

  defer(delayMs: number): void {
    this.pool.finishAttempt(this, this.group, delayMs);
  }

  release(): void {
    if (this.released) return;
    this.released = true;
    this.pool.release(this, this.group, this.bytes);
  }
}

export interface AdmissionProgress {
  attempt: number;
  elapsed_ms: number;
  retry_in_ms: number;
}

export type ResponsesEventSource = (
  state: StreamState,
  signal: AbortSignal,
  hooks: StreamHooks | undefined,
  onWait: (progress: AdmissionProgress) => void,
) => AsyncIterable<EngineEvent>;

interface AdmissionOptions {
  config: AdmissionConfig;
  pool: AdmissionPool;
  lease: AdmissionLease;
  open: (signal: AbortSignal) => Promise<Response>;
  idleMs: number;
  log?: (phase: string, fields: Record<string, unknown>) => void;
}

async function errorText(response: Response, signal: AbortSignal, idleMs: number): Promise<string> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  const cancel = (): void => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      signal.throwIfAborted();
      timer = setTimeout(() => { timedOut = true; cancel(); }, idleMs);
      const item = await reader.read();
      clearTimeout(timer);
      signal.throwIfAborted();
      if (timedOut) throw new Error("upstream error body idle timeout");
      if (item.done) break;
      bytes += item.value.byteLength;
      if (bytes > MAX_ERROR_BYTES) throw new Error("upstream error body exceeds admission inspection limit");
      chunks.push(item.value);
    }
    return Buffer.concat(chunks).toString("utf8");
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function admissionEventSource(opts: AdmissionOptions): ResponsesEventSource {
  return async function* (state, downstream, hooks, onWait) {
    const started = performance.now();
    const budget = new AbortController();
    const signal = AbortSignal.any([downstream, opts.pool.shutdown.signal, budget.signal]);
    let budgetTimer: ReturnType<typeof setTimeout> | undefined;
    if (opts.config.waitTimeoutMs > 0) {
      budgetTimer = setTimeout(() => budget.abort(admissionLimitError(
        "Admission wait budget exhausted; request is still not admitted",
      )), opts.config.waitTimeoutMs);
    }
    let attempt = 0;
    let lastProgressAt = started;
    let admitted = false;
    let generating = false;
    const log = (phase: string, fields: Record<string, unknown> = {}): void =>
      opts.log?.(phase, { attempt, elapsed_ms: Math.round(performance.now() - started), ...fields });
    const accept = (): void => {
      if (admitted) return;
      admitted = true;
      clearTimeout(budgetTimer);
      opts.lease.release();
      log("admission_accepted");
    };
    try {
      for (;;) {
        signal.throwIfAborted();
        let heartbeat: ReturnType<typeof setTimeout>;
        const progress = (): void => {
          lastProgressAt = performance.now();
          onWait({
            attempt,
            elapsed_ms: Math.round(lastProgressAt - started),
            retry_in_ms: opts.lease.retryInMs(),
          });
          heartbeat = setTimeout(progress, HEARTBEAT_MS);
          heartbeat.unref?.();
        };
        // Cover local backoff and the upstream header wait. Stop once headers
        // arrive so a stalled SSE is still caught by the idle timer.
        heartbeat = setTimeout(progress, Math.max(1, HEARTBEAT_MS - (performance.now() - lastProgressAt)));
        heartbeat.unref?.();
        try {
          await opts.lease.acquire(signal);
          signal.throwIfAborted();
          attempt++;
          log("admission_attempt");
          const attemptAbort = new AbortController();
          const attemptSignal = AbortSignal.any([signal, attemptAbort.signal]);
          let rejected = false;
          let retryAfter = 0;
          let response: Response | undefined;
          try {
            try {
              response = await opts.open(attemptSignal);
            } finally {
              clearTimeout(heartbeat);
            }
            signal.throwIfAborted();
            retryAfter = retryAfterMs(response.headers.get("retry-after"));
            if (!response.ok) {
              const text = await errorText(response, attemptSignal, opts.idleMs);
              const error = classifyUpstreamErrorEvent(text);
              rejected = ![401, 403].includes(response.status) &&
                error.category === "rate_limit" && error.code === UPSTREAM_ERROR_CODE_QUEUE_FULL;
              if (!rejected) {
                hooks?.onErrorEvent?.(text);
                const status = [401, 403].includes(response.status) ? response.status
                  : error.category === "quota" || error.category === "rate_limit" ? 429
                  : error.category === "invalid_request" ? 400 : response.status;
                const code = [401, 403].includes(status) ? "invalid_api_key"
                  : error.category === "quota" ? "insufficient_quota"
                  : status === 429 ? "rate_limit_exceeded"
                  : status === 400 ? "invalid_prompt"
                  : "upstream_http_error";
                throw new ApiError(error.message || `upstream returned ${status}`, status, openaiTypeForStatus(status), code);
              }
            } else {
              if (!response.body) throw new Error("upstream returned an empty body");
              for await (const event of runUpstreamStream(response.body, state, opts.idleMs, attemptSignal, {
                ...hooks,
                onActivity: () => { accept(); hooks?.onActivity?.(); },
                onErrorEvent: (raw) => {
                  const error = classifyUpstreamErrorEvent(raw);
                  if (!admitted && error.category === "rate_limit" && error.code === UPSTREAM_ERROR_CODE_QUEUE_FULL) {
                    return;
                  }
                  hooks?.onErrorEvent?.(raw);
                },
                onFinish: undefined,
              })) {
                signal.throwIfAborted();
                if (event.type === "errorEvent") {
                  const error = classifyUpstreamErrorEvent(event.raw);
                  rejected = !admitted && error.category === "rate_limit" && error.code === UPSTREAM_ERROR_CODE_QUEUE_FULL;
                  if (rejected) break;
                }
                if (event.type === "queue") log("upstream_queued", { position: event.queue.position ?? null });
                if (event.type === "delta" && !generating) { generating = true; log("generating"); }
                yield event;
                if (["terminal", "errorEvent", "protocolError", "readError"].includes(event.type)) return;
              }
            }
          } finally {
            // No overlapping attempts; close both the reader and the fetch abort linkage.
            await response?.body?.cancel().catch(() => {});
            attemptAbort.abort();
          }
          signal.throwIfAborted();
          if (!rejected) return;
          state.sawErrorEvent = false;
          state.errorEvent = "";
          const delayMs = Math.max(admissionDelay(opts.config, attempt), retryAfter);
          log("admission_rejected", { code: UPSTREAM_ERROR_CODE_QUEUE_FULL });
          opts.lease.defer(delayMs);
          log("admission_backoff", { retry_in_ms: delayMs });
        } finally {
          clearTimeout(heartbeat);
        }
      }
    } catch (error) {
      const cause = budget.signal.aborted ? budget.signal.reason : error;
      state.readError = cause instanceof Error ? cause : new Error(String(cause));
      if (cause instanceof CredentialPrincipalChangedError) {
        throw new ApiError("Credential principal changed; automatic admission stopped", 401, "authentication_error", "invalid_api_key");
      }
      throw cause;
    } finally {
      clearTimeout(budgetTimer);
      opts.lease.release();
      if (downstream.aborted || opts.pool.shutdown.signal.aborted) state.downstreamCanceled = true;
      const outcome = classifyOutcome(state);
      log(outcome === "downstream_canceled" ? "canceled" : outcome === "completed" ? "completed" : "failed", { outcome });
      hooks?.onFinish?.(outcome);
    }
  };
}
