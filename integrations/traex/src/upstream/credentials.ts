/**
 * Trae/Codebase JWT 凭证加载。
 *
 * 五种来源（互斥，由 config 决定）：环境变量 / 文件 / 自定义命令 / git / bytedcli。
 *
 * **git 是容器部署的主路径**：一条 `git ls-remote` 就能换出 14 天的 Codebase JWT，
 * 凭据只是 SSH key，所以镜像里只需要 git + openssh-client，`~/.ssh` 还能只读挂载。
 * 配合下面的过期自动重载，容器完全自治 —— 不需要任何外部定时任务往里灌 token。
 *
 * 文件来源仍然保留：宿主机把新 token 原子写入挂载文件，服务端按 mtime 感知变化
 * 并重读 —— 所以**不能只在启动时读一次**。
 *
 * JWT 有效期从 payload 的 `exp` 解析；解析不出来时保守假设 10 分钟后过期。
 * 上游任意接口返回 401 时走 `refresh()` 强制重载并重试一次。
 */

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { accessSync, constants } from "node:fs";
import { promisify } from "node:util";
import type { Config } from "../config.js";

const execFileAsync = promisify(execFile);

/** 距过期不足这个时间就主动重新加载 */
const REFRESH_SKEW_MS = 60_000;
/** 无法从 JWT 解析出 exp 时的保守有效期 */
const FALLBACK_TTL_MS = 10 * 60_000;

/**
 * 这个仓库存在的唯一目的就是签发 Codebase JWT：连上去必然被拒（403），
 * 但服务端会把 JWT 放在拒绝信息里一起返回。
 */
const CODEBASE_GIT_AUTH_REMOTE = "git@code.byted.org:builtin/cli-authenticate.git";
const CODEBASE_JWT_PATTERN = /(?:^|\r?\n)X-Code-JWT[:\s]+(\S+)/;

/**
 * 容器里通常没有 known_hosts，默认的 StrictHostKeyChecking 会停下来等人敲 yes，
 * 表现为请求一直挂着。accept-new 只自动接受**首次**见到的主机，host key 变了
 * 仍然拒绝，所以不像 `no` 那样等于关掉校验。BatchMode 保证任何交互提示
 * （比如 key 带 passphrase）都立刻失败而不是卡住。
 */
const DEFAULT_GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new";

/**
 * 从 git 的输出里抠出 Codebase JWT。
 *
 * 输出长这样（注意 git 本身是失败的，JWT 混在拒绝信息里）：
 *
 *     X-Code-API-Endpoint https://code.byted.org/api/v2/
 *     X-Code-JWT eyJhbGciOi...
 *     ---------------------
 *      Exit  | 403
 *     fatal: Could not read from remote repository.
 */
export function extractCodebaseJwt(output: string): string | null {
  return CODEBASE_JWT_PATTERN.exec(output)?.[1]?.trim() ?? null;
}

export class CredentialError extends Error {}

/**
 * 从凭证载荷里取出裸 JWT。
 *
 * 内容可能是裸 token，也可能是 JSON（bytedcli / ~/.trae/cli/auth.json 的形态）。
 */
export function parseTraeToken(data: string): string {
  const trimmed = data.trim();
  if (trimmed === "") throw new CredentialError("credential source returned empty data");
  if (!trimmed.startsWith("{")) return trimmed;

  let obj: unknown;
  try {
    obj = JSON.parse(trimmed);
  } catch {
    throw new CredentialError("credential source returned malformed JSON");
  }
  if (typeof obj !== "object" || obj === null) {
    throw new CredentialError("credential JSON does not contain an access token");
  }
  const o = obj as Record<string, unknown>;
  const trae = o["trae"];
  const candidates: unknown[] = [
    typeof trae === "object" && trae !== null ? (trae as Record<string, unknown>)["access_token"] : undefined,
    o["access_token"],
    o["token"],
    o["jwt"],
  ];
  for (const c of candidates) {
    if (typeof c === "string" && c.trim() !== "") return c.trim();
  }
  throw new CredentialError("credential JSON does not contain an access token");
}

/**
 * 从 JWT payload 解析过期时间。任何一步失败都退回 now + 10min，
 * 让调用方按短有效期处理而不是直接失败。
 */
export function tokenExpiry(token: string, now = Date.now()): number {
  const parts = token.split(".");
  if (parts.length < 2) return now + FALLBACK_TTL_MS;
  try {
    // base64url，无 padding
    const payload = Buffer.from(parts[1]!, "base64url").toString("utf8");
    const obj = JSON.parse(payload) as { exp?: unknown };
    const exp = obj.exp;
    if (typeof exp === "number" && exp > 0) return exp * 1000;
  } catch {
    /* 落到 fallback */
  }
  return now + FALLBACK_TTL_MS;
}

/**
 * 从可信凭证来源返回的 JWT 中提取稳定主体并做不可逆摘要。
 * 不把完整 token 纳入摘要，保证同账号正常轮换仍可安全重试；账号变化时上游客户端
 * 会停止自动 401 重试，避免一次请求在用户不知情时切换认证主体。
 */
export function tokenPrincipalScope(token: string): string {
  const parts = token.split(".");
  if (parts.length < 2) return "";
  try {
    const payload = JSON.parse(
      Buffer.from(parts[1]!, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    const issuer = typeof payload["iss"] === "string" ? payload["iss"].trim() : "";
    const subject = typeof payload["sub"] === "string" ? payload["sub"].trim() : "";
    const audience = Array.isArray(payload["aud"])
      ? payload["aud"].filter((v): v is string => typeof v === "string").sort()
      : typeof payload["aud"] === "string"
        ? [payload["aud"]]
        : [];
    if (issuer === "" || subject === "") return "";
    return createHash("sha256")
      .update("traex-credential-principal-v1", "utf8")
      .update("\u0000", "utf8")
      .update(JSON.stringify({ issuer, subject, audience }), "utf8")
      .digest("base64url");
  } catch {
    return "";
  }
}

function findBytedcli(explicit: string): string {
  if (explicit !== "") return explicit;
  const candidates = [
    process.env["BYTEDCLI"],
    ...(process.env["PATH"] ?? "")
      .split(":")
      .filter(Boolean)
      .map((dir) => `${dir}/bytedcli`),
    `${process.env["HOME"] ?? ""}/.local/bin/bytedcli`,
  ].filter((p): p is string => typeof p === "string" && p !== "");

  for (const c of candidates) {
    try {
      accessSync(c, constants.X_OK);
      return c;
    } catch {
      /* 继续找 */
    }
  }
  throw new CredentialError(
    "bytedcli not found on PATH and no TRAEX_BRIDGE_TOKEN / _TOKEN_FILE / _TOKEN_COMMAND configured",
  );
}

interface CachedToken {
  token: string;
  expiresAt: number;
  /** 文件来源：读到该 token 时文件的 mtime，用于感知外部刷新 */
  mtimeMs?: number;
}

export class CredentialProvider {
  private cached: CachedToken | null = null;
  private inflight: Promise<string> | null = null;

  constructor(private readonly cfg: Config) {}

  /**
   * 当前凭证距过期还有几天。没加载过或解析不出 exp 时返回 null。
   * 用于过期预警 —— token 静默过期后表现为一堆 401，很难定位到根因。
   */
  expiresInDays(): number | null {
    if (!this.cached) return null;
    return (this.cached.expiresAt - Date.now()) / 86_400_000;
  }

  /** 描述当前凭证来源，用于启动日志（不含 token 内容） */
  describe(): string {
    switch (this.cfg.credentialSource) {
      case "environment": return "environment variable";
      case "file": return `file ${this.cfg.traeTokenFile}`;
      case "command": return "custom command";
      case "git": return "git ssh identity (auto-renewing)";
      case "bytedcli": return "bytedcli auth get-codebase-jwt-token";
      default: return this.cfg.credentialSource;
    }
  }

  /**
   * 取当前有效 token。缓存未过期（且文件未被外部刷新）时直接复用。
   */
  async token(): Promise<string> {
    const now = Date.now();
    const c = this.cached;
    if (c && now + REFRESH_SKEW_MS < c.expiresAt) {
      // 文件来源额外检查 mtime：宿主机可能已经写入了新 token
      if (this.cfg.credentialSource !== "file") return c.token;
      const m = await this.fileMtime();
      if (m !== null && m === c.mtimeMs) return c.token;
    }
    return this.reload();
  }

  /**
   * 上游返回 401 后强制刷新。
   *
   * 若缓存里的 token 已经不是被拒绝的那个（说明并发请求里别人已经刷过了），
   * 直接复用，避免刷新风暴。
   */
  async refresh(rejectedToken: string): Promise<string> {
    const c = this.cached;
    if (c && c.token !== rejectedToken && Date.now() < c.expiresAt) return c.token;
    this.cached = null;
    return this.reload();
  }

  private async fileMtime(): Promise<number | null> {
    try {
      const st = await fs.stat(this.cfg.traeTokenFile);
      return st.mtimeMs;
    } catch {
      return null;
    }
  }

  /** 单飞：并发调用共享同一次加载 */
  private reload(): Promise<string> {
    if (this.inflight) return this.inflight;
    const p = this.load()
      .finally(() => {
        this.inflight = null;
      });
    this.inflight = p;
    return p;
  }

  private async load(): Promise<string> {
    const { raw, mtimeMs } = await this.readRaw();
    const token = parseTraeToken(raw);

    // 合法 JWT 不含空白字符；含空白说明读到了半截文件或命令输出混入了别的东西
    if (/\s/.test(token)) {
      throw new CredentialError("credential source returned malformed token data (contains whitespace)");
    }

    this.cached = {
      token,
      expiresAt: tokenExpiry(token),
      ...(mtimeMs !== undefined ? { mtimeMs } : {}),
    };
    return token;
  }

  private async readRaw(): Promise<{ raw: string; mtimeMs?: number }> {
    const cfg = this.cfg;
    switch (cfg.credentialSource) {
      case "environment":
        return { raw: cfg.traeToken };

      case "file": {
        let raw: string;
        try {
          raw = await fs.readFile(cfg.traeTokenFile, "utf8");
        } catch (e) {
          throw new CredentialError(`failed to read token file ${cfg.traeTokenFile}: ${(e as Error).message}`);
        }
        const m = await this.fileMtime();
        return m === null ? { raw } : { raw, mtimeMs: m };
      }

      case "command":
        return { raw: await this.runCommand("/bin/sh", ["-c", cfg.traeTokenCommand]) };

      case "git":
        return { raw: await this.runGitAuth() };

      case "bytedcli": {
        const bin = findBytedcli(cfg.bytedcliBin);
        return { raw: await this.runCommand(bin, ["auth", "get-codebase-jwt-token"]) };
      }

      case "trae-auth-file-compat": {
        const p = `${process.env["HOME"] ?? ""}/.trae/cli/auth.json`;
        try {
          return { raw: await fs.readFile(p, "utf8") };
        } catch (e) {
          throw new CredentialError(`failed to read ${p}: ${(e as Error).message}`);
        }
      }

      default:
        throw new CredentialError(`unknown credential source: ${cfg.credentialSource}`);
    }
  }

  /**
   * 用 SSH 身份换一个 Codebase JWT。
   *
   * 这里**不能**复用 runCommand：`git ls-remote` 对这个仓库一定以非零码退出
   * （403 - 仓库本来就不给读），而我们要的 JWT 恰恰在那次失败的输出里。
   * 所以成功和失败两条路都得抠一次，只有两边都没抠到才算真失败。
   */
  private async runGitAuth(): Promise<string> {
    const remote = process.env["TRAEX_BRIDGE_GIT_AUTH_REMOTE"] || CODEBASE_GIT_AUTH_REMOTE;
    const opts = {
      timeout: this.cfg.tokenCommandTimeoutMs,
      maxBuffer: 1024 * 1024,
      encoding: "utf8" as const,
      env: {
        ...process.env,
        GIT_TERMINAL_PROMPT: "0",
        GIT_SSH_COMMAND: process.env["GIT_SSH_COMMAND"] || DEFAULT_GIT_SSH_COMMAND,
      },
    };

    let combined: string;
    let failure = "";
    try {
      const { stdout, stderr } = await execFileAsync("git", ["ls-remote", remote], opts);
      combined = `${stdout}\n${stderr}`;
    } catch (e) {
      const err = e as { stdout?: string; stderr?: string; message?: string };
      combined = `${err.stdout ?? ""}\n${err.stderr ?? ""}`;
      failure = err.message ?? "unknown error";
    }

    const jwt = extractCodebaseJwt(combined);
    if (jwt) return jwt;

    // 到这儿说明连 JWT 都没拿到，多半是 SSH 身份不可用（没挂 ~/.ssh、key 带
    // passphrase、或者 known_hosts 校验失败）。把 git 的原话带出去，否则
    // 用户只会看到一句无从下手的 "credential command failed"。
    const detail = combined.trim().slice(0, 500);
    throw new CredentialError(
      `git credential exchange did not return a Codebase JWT` +
        `${failure ? ` (${failure})` : ""}` +
        `${detail ? ` — ${detail}` : ""}`,
    );
  }

  private async runCommand(cmd: string, args: string[]): Promise<string> {
    try {
      const { stdout } = await execFileAsync(cmd, args, {
        timeout: this.cfg.tokenCommandTimeoutMs,
        maxBuffer: 1024 * 1024,
        encoding: "utf8",
      });
      return stdout;
    } catch (e) {
      const err = e as { message?: string; stderr?: string };
      const stderr = (err.stderr ?? "").trim();
      throw new CredentialError(
        `credential command failed: ${err.message ?? "unknown error"}${stderr ? ` — ${stderr.slice(0, 500)}` : ""}`,
      );
    }
  }
}
