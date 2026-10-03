/** Managed-service metadata only. Never retain request bodies, arguments or output. */
import { appendFileSync, chmodSync, existsSync, lstatSync, mkdirSync, renameSync } from "node:fs";
import { join } from "node:path";
import type { FastifyInstance, FastifyRequest } from "fastify";
import type { CatalogModel } from "./upstream/catalog.js";
import type { UpstreamUsage } from "./types/unified.js";

export const ASTRA_MODELS = new Set(["GPT-6-Astra", "GPT-6-Astra[1m]"]);
export const DEFAULT_MODEL = "GPT-6-Astra[1m]";
const PATHS = new Set(["/health", "/healthz", "/readyz", "/v1/models", "/models",
  "/v1/responses", "/responses", "/v1/usage", "/usage", "/admin/drain", "/admin/resume"]);
type Run = { model: string; state: string; started: number; last: number; events: number;
  handler: boolean; inference: boolean; outcome: string; queueMs: number; queueStarted?: number;
  usage?: UpstreamUsage; stream: boolean; promptBytes: number };

export class BridgeRuntime {
  draining = false;
  readonly runs = new Map<string, Run>();
  constructor(readonly version: string, readonly logDir?: string, readonly maxRequests = 8) {}

  install(app: FastifyInstance): void {
    app.addHook("onRequest", async (req, reply) => {
      const path = (req.raw.url ?? "").split("?")[0]!;
      const host = (req.headers.host ?? "").replace(/:\d+$/, "");
      if (!["127.0.0.1", "localhost", "[::1]"].includes(host)) {
        return reply.code(403).send({ error: { message: "Loopback Host required" } });
      }
      if (!PATHS.has(path)) return reply.code(404).send({ error: { message: "Not found" } });
      if (path.startsWith("/admin/") && req.headers.origin) {
        return reply.code(403).send({ error: { message: "Administrative requests must not have an Origin" } });
      }
      if (req.method === "POST" && ["/v1/responses", "/responses"].includes(path)) {
        if (this.draining) return reply.code(503).send({ error: { message: "Service draining", code: "draining" } });
        if (this.runs.size >= this.maxRequests) return reply.code(429).send({ error: { message: "Local admission capacity full", code: "rate_limit_exceeded" } });
        const now = performance.now();
        this.runs.set(req.id, { model: DEFAULT_MODEL, state: "receiving", started: now, last: now,
          events: 0, handler: false, inference: false, outcome: "invalid_request", queueMs: 0,
          stream: false, promptBytes: 0 });
        reply.raw.once("close", () => { if (!this.runs.get(req.id)?.handler) this.end(req); });
      }
    });
    app.addHook("onResponse", async (req) => { if (!this.runs.get(req.id)?.handler) this.end(req); });
    app.post("/admin/drain", async (req, reply) => {
      // The app's API-key hook also authenticates administration.
      const force = (req.query as Record<string, unknown>)["force"] === "1";
      if (this.runs.size && !force) return reply.code(409).send({ draining: false, unfinished: this.runs.size });
      this.draining = true;
      return { draining: true, unfinished: this.runs.size };
    });
    app.post("/admin/resume", async () => {
      this.draining = false;
      return { draining: false, unfinished: this.runs.size };
    });
  }

  begin(req: FastifyRequest, model: string, stream: boolean): void {
    const run = this.runs.get(req.id);
    if (!run) throw new Error("Missing admission record");
    Object.assign(run, { handler: true, model, stream, state: "running", outcome: "upstream_incomplete",
      promptBytes: Buffer.byteLength(JSON.stringify(req.body)) });
  }
  event(id: string, kind: "queue" | "usage" | "start" | "finish" | "error" | "activity", value?: unknown): void {
    const run = this.runs.get(id);
    if (!run) return;
    const now = performance.now();
    run.events++;
    run.last = now;
    if (kind === "start") run.inference = true;
    if (kind === "queue") { run.state = "queued"; run.queueStarted ??= now; }
    if (kind === "usage" || kind === "finish" || kind === "activity") {
      if (run.queueStarted !== undefined) { run.queueMs += now - run.queueStarted; delete run.queueStarted; }
      run.state = "running";
    }
    if (kind === "usage") run.usage = value as UpstreamUsage;
    if (kind === "error") run.outcome = "upstream_error";
    if (kind === "finish") run.outcome = value === "completed" ? "completed" : value === "downstream_canceled" ? "client_disconnected" :
      typeof value === "string" && /^upstream_[a-z_]+$/.test(value) ? value : "upstream_incomplete";
  }
  end(req: FastifyRequest): void {
    const run = this.runs.get(req.id);
    if (!run) return;
    this.runs.delete(req.id);
    const now = performance.now();
    const base = { ts: new Date().toISOString(), api: "responses", model: run.model, stream: run.stream,
      prompt_bytes: run.promptBytes, outcome: run.outcome, duration_s: (now - run.started) / 1000,
      queue_s: (run.queueMs + (run.queueStarted === undefined ? 0 : now - run.queueStarted)) / 1000 };
    if (run.inference) this.record({ ...base, event: "inference", usage: run.usage ? {
      input_tokens: run.usage.prompt_tokens, output_tokens: run.usage.completion_tokens,
      input_tokens_details: { cached_tokens: run.usage.cache_read_input_tokens },
      cache_read_tokens: run.usage.cache_read_input_tokens,
      cache_write_tokens: run.usage.cache_write_input_tokens } : undefined });
    this.record({ ...base, event: "http" });
  }
  health(models: readonly CatalogModel[], limits: Record<string, unknown>): Record<string, unknown> {
    const now = performance.now();
    const runs = [...this.runs.values()].map(r => ({ model: r.model, state: r.state, events: r.events,
      running_s: (now - r.started) / 1000, idle_s: (now - r.last) / 1000 }));
    return { service: "traex-bridge", status: models.some(m => m.id === DEFAULT_MODEL) ? "ok" : "unavailable",
      adapter_version: 1, running_version: this.version, upstream_version: "0.6.4",
      generation_mode: "stateless", capabilities: ["responses", "namespace_functions", "image_inputs", "request_progress", "drain"],
      draining: this.draining, unfinished: runs.length, limits,
      progress: { active: runs[0] ?? null, runs, queued: runs.filter(r => r.state === "queued").length,
        unfinished: runs.length, pending_tools: 0 } };
  }
  private record(row: Record<string, unknown>): void {
    if (!this.logDir) return;
    try {
      mkdirSync(this.logDir, { recursive: true, mode: 0o700 });
      if (lstatSync(this.logDir).isSymbolicLink()) return;
      const path = join(this.logDir, "requests.jsonl");
      if (existsSync(path) && (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink())) return;
      if (existsSync(path) && lstatSync(path).size > 1024 * 1024) {
        for (let i = 2; i >= 1; i--) if (existsSync(`${path}.${i}`)) renameSync(`${path}.${i}`, `${path}.${i + 1}`);
        renameSync(path, `${path}.1`);
      }
      appendFileSync(path, JSON.stringify(row) + "\n", { mode: 0o600 });
      chmodSync(path, 0o600);
    } catch { /* Logging must not break inference; never print raw exceptions. */ }
  }
}
