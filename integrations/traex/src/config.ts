/**
 * 配置解析与校验。
 *
 * 所有配置来自环境变量，启动时一次性解析并 fail-fast —— 错误的配置在启动时炸，
 * 而不是等到第一个请求才炸。
 *
 * bridge 自有变量统一用 `TRAEX_BRIDGE_` 前缀；`TRAE_PPE_ENV` 为了与 TraeX
 * 启动方式直接兼容，刻意保留其原名。
 */

import { accessSync, constants, existsSync, readdirSync } from "node:fs";
import { isIP } from "node:net";
import { normalizeTraePpeEnv } from "./upstream/ppe.js";

/** TraeX ingress is fronted by TLB with `client_max_body_size 1g`. */
export const TRAE_UPSTREAM_MAX_REQUEST_BYTES = 1024 * 1024 * 1024;

/** git 是否可执行 —— 没有它就别选中 git 来源，让它回退到 bytedcli */
function hasGit(): boolean {
  for (const dir of (process.env["PATH"] ?? "").split(":").filter(Boolean)) {
    try {
      accessSync(`${dir}/git`, constants.X_OK);
      return true;
    } catch {
      /* 继续找 */
    }
  }
  return false;
}

/**
 * 是否具备可用的 SSH 身份。
 *
 * 只做存在性判断，不验证 key 真的能连上 —— 真连不上时 CredentialProvider
 * 会带着 git 的原始报错抛出来，比在这里猜测更有用。
 */
function hasSshIdentity(): boolean {
  if ((process.env["SSH_AUTH_SOCK"] ?? "") !== "") return true;
  const home = process.env["HOME"] ?? "";
  if (home === "") return false;
  try {
    const entries = readdirSync(`${home}/.ssh`);
    // 私钥的标准命名是 id_*；~/.ssh/config 里也可能用 IdentityFile 指别处
    return entries.some((f) => f.startsWith("id_") && !f.endsWith(".pub")) || entries.includes("config");
  } catch {
    return false;
  }
}

export type CredentialSource =
  | "environment"
  | "file"
  | "command"
  | "git"
  | "bytedcli"
  | "trae-auth-file-compat";

export interface AdmissionConfig {
  enabled: boolean;
  initialDelayMs: number;
  maxDelayMs: number;
  waitTimeoutMs: number;
  maxRequests: number;
  maxBytes: number;
}

export function loadAdmissionConfig(env: NodeJS.ProcessEnv = process.env): AdmissionConfig {
  const prefix = "TRAEX_BRIDGE_ADMISSION_";
  const duration = (name: string, fallback: number, allowZero = false): number => {
    const rawMs = env[`${prefix}${name}_MS`];
    const raw = env[`${prefix}${name}`];
    const value = rawMs !== undefined ? (/^\d+$/.test(rawMs) ? Number(rawMs) : NaN)
      : raw !== undefined ? parseDurationMs(raw, prefix + name) : fallback;
    if (!Number.isSafeInteger(value) || value < (allowZero ? 0 : 1) || value > 2_147_483_647) {
      throw new Error(`${prefix}${name} is outside the supported timer range`);
    }
    return value;
  };
  const integer = (name: string, fallback: number): number => {
    const raw = env[prefix + name];
    const value = raw === undefined ? fallback : /^\d+$/.test(raw) ? Number(raw) : NaN;
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${prefix}${name} must be a positive integer`);
    return value;
  };
  const enabled = env[prefix + "ENABLED"] ?? "true";
  if (!["true", "false", "1", "0"].includes(enabled)) {
    throw new Error(`${prefix}ENABLED must be true/false or 1/0`);
  }
  const config = {
    enabled: enabled === "true" || enabled === "1",
    initialDelayMs: duration("INITIAL_DELAY", 30_000),
    maxDelayMs: duration("MAX_DELAY", 120_000),
    waitTimeoutMs: duration("WAIT_TIMEOUT", 6 * 3600_000, true),
    maxRequests: integer("MAX_REQUESTS", 8),
    maxBytes: integer("MAX_BYTES", TRAE_UPSTREAM_MAX_REQUEST_BYTES),
  };
  if (config.maxDelayMs < config.initialDelayMs) throw new Error(`${prefix}MAX_DELAY must be >= INITIAL_DELAY`);
  return config;
}

export function loadResponsesDeferLifecycle(env: NodeJS.ProcessEnv = process.env): boolean {
  const name = "TRAEX_BRIDGE_RESPONSES_DEFER_LIFECYCLE";
  const raw = env[name] ?? "false";
  if (!["true", "false", "1", "0"].includes(raw)) {
    throw new Error(`${name} must be true/false or 1/0`);
  }
  return raw === "true" || raw === "1";
}

export interface Config {
  /** 进程实际监听的容器/本机地址；与 Docker 发布到宿主机的地址无关 */
  host: string;
  /** 进程实际监听的容器/本机端口；与 Docker 发布到宿主机的端口无关 */
  port: number;
  /** 下游 API key；空字符串表示不鉴权（仅允许监听 loopback 时） */
  apiKey: string;
  /** 请求未指定 model、或 model 既非 catalog id 也非 family alias 时的兜底 */
  defaultModel: string;

  /** chat + quota 上游 */
  upstreamBaseUrl: string;
  /** catalog 上游，默认与 chat 同源 */
  catalogBaseUrl: string;
  /** 对齐 TraeX 的 TRAE_PPE_ENV；空字符串表示默认（非 PPE）通道 */
  traePpeEnv: string;

  credentialSource: CredentialSource;
  traeToken: string;
  traeTokenFile: string;
  traeTokenCommand: string;
  bytedcliBin: string;
  tokenCommandTimeoutMs: number;

  /** API key 和可选凭证文件的持久化目录；不保存生成请求或会话状态。 */
  stateDir: string;

  maxRequestBytes: number;

  /** 等上游 SSE 响应头的最长时间。排队时上游可能十几分钟才吐第一个字节 */
  upstreamHeaderTimeoutMs: number;
  /** 上游 SSE 两次数据之间的最大间隔 */
  streamIdleTimeoutMs: number;
  /** Bounded, connection-local admission retries for streaming Responses. */
  admission?: AdmissionConfig;
  responsesDeferLifecycle: boolean;

  logLevel: string;
  logRequests: boolean;
  dumpBody: boolean;
  trace: boolean;
}

function firstEnv(...names: string[]): string {
  for (const name of names) {
    const v = process.env[name];
    if (v !== undefined && v !== "") return v;
  }
  return "";
}

function firstEnvOr(fallback: string, ...names: string[]): string {
  const v = firstEnv(...names);
  return v === "" ? fallback : v;
}

function envFlag(...names: string[]): boolean {
  const v = firstEnv(...names);
  if (v === "") return false;
  const s = v.trim().toLowerCase();
  return s === "1" || s === "true" || s === "yes" || s === "on";
}

/** 非空即真的调试开关 */
function envPresent(...names: string[]): boolean {
  return firstEnv(...names) !== "";
}

/**
 * 解析 Go 风格的 duration（`10m` / `90s` / `1500ms` / `2h`）或纯秒数。
 * 返回毫秒。
 */
export function parseDurationMs(raw: string, name: string): number {
  const v = raw.trim();
  if (v === "") throw new Error(`${name} must not be empty`);
  if (/^-?\d+$/.test(v)) return Number(v) * 1000; // 纯数字按秒
  const m = /^(-?\d+(?:\.\d+)?)(ms|s|m|h)$/.exec(v);
  if (!m) throw new Error(`${name} must be a duration like 10m/90s/1500ms, got ${JSON.stringify(raw)}`);
  const n = Number(m[1]);
  switch (m[2]) {
    case "ms": return n;
    case "s": return n * 1000;
    case "m": return n * 60_000;
    case "h": return n * 3_600_000;
    default: throw new Error(`${name}: unreachable unit`);
  }
}

export function loadMaxRequestBytes(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env["TRAEX_BRIDGE_MAX_REQUEST_BYTES"];
  if (raw === undefined || raw === "") return TRAE_UPSTREAM_MAX_REQUEST_BYTES;
  if (!/^-?\d+$/.test(raw.trim())) {
    throw new Error(`TRAEX_BRIDGE_MAX_REQUEST_BYTES must be an integer, got ${JSON.stringify(raw)}`);
  }
  const configured = Number(raw.trim());
  if (
    !Number.isSafeInteger(configured) ||
    configured < 0 ||
    configured > TRAE_UPSTREAM_MAX_REQUEST_BYTES
  ) {
    throw new Error(
      `TRAEX_BRIDGE_MAX_REQUEST_BYTES must be between 0 and ${TRAE_UPSTREAM_MAX_REQUEST_BYTES}`,
    );
  }
  // Preserve the historical `0` spelling, but keep it bounded to the upstream ingress limit.
  return configured === 0 ? TRAE_UPSTREAM_MAX_REQUEST_BYTES : configured;
}

function envDurationMs(fallbackMs: number, name: string, ...aliases: string[]): number {
  // `<NAME>_MS` 变体按毫秒整数解释
  const msRaw = firstEnv(`${name}_MS`, ...aliases.map((a) => `${a}_MS`));
  if (msRaw !== "") {
    if (!/^\d+$/.test(msRaw.trim())) throw new Error(`${name}_MS must be a non-negative integer`);
    return Number(msRaw.trim());
  }
  const raw = firstEnv(name, ...aliases);
  if (raw === "") return fallbackMs;
  return parseDurationMs(raw, name);
}

function homeDir(): string {
  return process.env.HOME || process.env.USERPROFILE || "/tmp";
}

export function isLoopbackHost(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (h === "localhost" || h === "::1") return true;
  if (isIP(h) !== 4) return false;
  return Number(h.split(".", 1)[0]) === 127;
}

/**
 * 上游 URL 必须是绝对 https；http 仅在 loopback 主机上允许（本地抓包调试）。
 */
function safeUpstreamUrl(raw: string, name: string): string {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    throw new Error(`${name} must be an absolute URL, got ${JSON.stringify(raw)}`);
  }
  if (u.protocol === "https:") return raw.replace(/\/+$/, "");
  if (u.protocol === "http:" && isLoopbackHost(u.hostname)) return raw.replace(/\/+$/, "");
  throw new Error(`${name} must use https (http allowed only for loopback hosts), got ${JSON.stringify(raw)}`);
}

export interface ListenAddress {
  host: string;
  port: number;
}

/**
 * 解析进程自身的监听地址。
 *
 * Docker 的宿主机发布地址/端口只应由 `docker run -p` 或 Compose 的
 * `TRAEX_BRIDGE_PUBLISH_*` 控制，绝不能进入这里影响容器内监听端口。
 * `TRAEX_BRIDGE_HOST` / `TRAEX_BRIDGE_PORT` 仅作为旧版的进程监听配置保留。
 */
export function parseListenAddress(env: NodeJS.ProcessEnv = process.env): ListenAddress {
  const value = (...names: string[]): string => {
    for (const name of names) {
      const current = env[name];
      if (current !== undefined && current !== "") return current;
    }
    return "";
  };

  const addr = value("TRAEX_BRIDGE_ADDR");
  let host: string;
  let port: number;
  if (addr !== "") {
    const idx = addr.lastIndexOf(":");
    if (idx < 0) throw new Error(`TRAEX_BRIDGE_ADDR must be host:port, got ${JSON.stringify(addr)}`);
    host = addr.slice(0, idx) || "0.0.0.0";
    const portRaw = addr.slice(idx + 1);
    if (!/^\d+$/.test(portRaw)) throw new Error(`TRAEX_BRIDGE_ADDR port must be numeric, got ${JSON.stringify(addr)}`);
    port = Number(portRaw);
  } else {
    host = value("TRAEX_BRIDGE_HOST") || "127.0.0.1";
    const portRaw = value("TRAEX_BRIDGE_PORT");
    if (portRaw !== "" && !/^-?\d+$/.test(portRaw.trim())) {
      throw new Error(`TRAEX_BRIDGE_PORT must be an integer, got ${JSON.stringify(portRaw)}`);
    }
    port = portRaw === "" ? 8317 : Number(portRaw.trim());
  }
  if (port <= 0 || port > 65535) throw new Error(`port out of range: ${port}`);
  return { host, port };
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  // ---- 监听地址 ----
  const { host, port } = parseListenAddress(env);

  // 未设时不再拒绝启动 —— 启动流程会自动生成一个并持久化（见 ensureApiKey）。
  // 目标是 `docker run` 不带任何 -e 也能安全地跑起来。
  const apiKey = firstEnv("TRAEX_BRIDGE_API_KEY");

  // 默认模型选一个实测不排队、不计入 metered 额度池的。GPT 系在上游经常排队
  // （实测队列位置 900+），不适合作为「请求没指定 model」时的兜底。
  const defaultModel = firstEnvOr(
    "DeepSeek-V4-Pro",
    "TRAEX_BRIDGE_DEFAULT_MODEL",
  );

  const upstreamBaseUrl = safeUpstreamUrl(
    firstEnvOr(
      "https://copilot-cn.bytedance.net",
      "TRAEX_BRIDGE_UPSTREAM",
    ),
    "TRAEX_BRIDGE_UPSTREAM",
  );

  const catalogRaw = firstEnv(
    "TRAEX_BRIDGE_CATALOG_UPSTREAM",
  );
  const catalogBaseUrl =
    catalogRaw === "" ? upstreamBaseUrl : safeUpstreamUrl(catalogRaw, "TRAEX_BRIDGE_CATALOG_UPSTREAM");

  // 与 TraeX 使用同名变量，便于在本机、Docker 和 Compose 之间原样迁移配置。
  const traePpeEnv = normalizeTraePpeEnv(env["TRAE_PPE_ENV"]);

  // 状态目录要先算出来 —— 凭证自动发现依赖它
  const stateDir = firstEnvOr(
    `${homeDir()}/.local/state/traex-bridge`,
    "TRAEX_BRIDGE_STATE_DIR",
  );

  // ---- 凭证来源：三种显式来源互斥 ----
  const traeToken = firstEnv("TRAEX_BRIDGE_TOKEN");
  let traeTokenFile = firstEnv("TRAEX_BRIDGE_TOKEN_FILE");
  const traeTokenCommand = firstEnv("TRAEX_BRIDGE_TOKEN_COMMAND");

  const explicit = [
    ["TRAEX_BRIDGE_TOKEN", traeToken],
    ["TRAEX_BRIDGE_TOKEN_FILE", traeTokenFile],
    ["TRAEX_BRIDGE_TOKEN_COMMAND", traeTokenCommand],
  ].filter(([, v]) => v !== "");

  if (explicit.length > 1) {
    throw new Error(
      `configure only one of TRAEX_BRIDGE_TOKEN / _TOKEN_FILE / _TOKEN_COMMAND, got: ${explicit
        .map(([k]) => k)
        .join(", ")}`,
    );
  }

  let credentialSource: CredentialSource;
  let bytedcliBin = "";
  if (traeToken !== "") {
    credentialSource = "environment";
  } else if (traeTokenFile !== "") {
    credentialSource = "file";
  } else if (traeTokenCommand !== "") {
    credentialSource = "command";
  } else if (existsSync(`${stateDir}/trae-token`)) {
    // 自动发现：有人往挂载目录里放了凭证文件就用它。
    // 这条主要是兼容老部署，新部署走下面的 git 自换。
    traeTokenFile = `${stateDir}/trae-token`;
    credentialSource = "file";
  } else if (hasGit() && hasSshIdentity()) {
    // 容器部署的主路径：一条 git ls-remote 就能换出 14 天的 JWT，过期后
    // CredentialProvider 会自己再换一次。用户只要把 ~/.ssh 只读挂进来，
    // 不需要任何定时任务，也不需要镜像里有 bytedcli。
    credentialSource = "git";
  } else {
    // 没有 SSH 身份时回退到 bytedcli（本机开发的常见情形）
    bytedcliBin = firstEnv("TRAEX_BRIDGE_BYTEDCLI_BIN", "BYTEDCLI");
    credentialSource = "bytedcli";
  }

  const tokenCommandTimeoutMs = envDurationMs(
    120_000,
    "TRAEX_BRIDGE_TOKEN_COMMAND_TIMEOUT",
  );
  if (tokenCommandTimeoutMs <= 0) throw new Error("TRAEX_BRIDGE_TOKEN_COMMAND_TIMEOUT must be > 0");

  const maxRequestBytes = loadMaxRequestBytes(env);

  const upstreamHeaderTimeoutMs = envDurationMs(
    600_000,
    "TRAEX_BRIDGE_UPSTREAM_HEADER_TIMEOUT",
  );
  if (upstreamHeaderTimeoutMs <= 0) throw new Error("TRAEX_BRIDGE_UPSTREAM_HEADER_TIMEOUT must be > 0");

  const streamIdleTimeoutMs = envDurationMs(
    600_000,
    "TRAEX_BRIDGE_STREAM_IDLE_TIMEOUT",
  );
  if (streamIdleTimeoutMs <= 0) throw new Error("TRAEX_BRIDGE_STREAM_IDLE_TIMEOUT must be > 0");


  return {
    host,
    port,
    apiKey,
    defaultModel,
    upstreamBaseUrl,
    catalogBaseUrl,
    traePpeEnv,
    credentialSource,
    traeToken,
    traeTokenFile,
    traeTokenCommand,
    bytedcliBin,
    tokenCommandTimeoutMs,
    stateDir,
    maxRequestBytes,
    upstreamHeaderTimeoutMs,
    streamIdleTimeoutMs,
    admission: loadAdmissionConfig(env),
    responsesDeferLifecycle: loadResponsesDeferLifecycle(env),
    logLevel: firstEnvOr("info", "TRAEX_BRIDGE_LOG_LEVEL", "LOG_LEVEL"),
    logRequests: envPresent("TRAEX_BRIDGE_LOG_REQUESTS"),
    dumpBody: envPresent("TRAEX_BRIDGE_DUMP_BODY"),
    trace: envPresent("TRAEX_BRIDGE_TRACE"),
  };
}
