import { randomBytes } from "node:crypto";
import {
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  ftruncateSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  type Stats,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import type { Config } from "./config.js";
import { isLoopbackHost } from "./config.js";

type ApiKeyConfig = Pick<Config, "apiKey" | "host" | "stateDir">;

export function apiKeyFilePath(stateDir: string): string {
  return join(stateDir, "api-key");
}

function generatedApiKey(): string {
  return `tb_${randomBytes(24).toString("hex")}`;
}

function ownerIsAllowed(fileUid: number, effectiveUid: number | undefined): boolean {
  // The image entrypoint normally drops to the bind mount owner. If it deliberately
  // remains root (for example with a root-owned named volume), root may safely read a
  // 0600 host-owned file. Non-root processes must own the secret themselves.
  return effectiveUid === undefined || effectiveUid === 0 || fileUid === effectiveUid;
}

function validateRegularOwner(
  keyPath: string,
  info: Stats,
  effectiveUid: number | undefined,
): void {
  if (info.isSymbolicLink() || !info.isFile()) {
    throw new Error(`API key path must be a regular non-symlink file: ${keyPath}`);
  }
  if (!ownerIsAllowed(info.uid, effectiveUid)) {
    throw new Error(
      `API key file must be owned by effective uid ${effectiveUid}, got uid ${info.uid}: ${keyPath}`,
    );
  }
}

function readOrPopulateExistingKey(
  keyPath: string,
  effectiveUid: number | undefined,
): string {
  const before = lstatSync(keyPath);
  validateRegularOwner(keyPath, before, effectiveUid);

  // Keep one descriptor from validation through initialization. Opening with
  // O_TRUNC in a second step would let a pathname swap truncate an attacker-
  // selected file before its inode could be checked.
  const fd = openSync(keyPath, constants.O_RDWR | constants.O_NOFOLLOW);
  try {
    const opened = fstatSync(fd);
    validateRegularOwner(keyPath, opened, effectiveUid);
    if (opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error(`API key file changed while being validated: ${keyPath}`);
    }
    const content = readFileSync(fd, "utf8").trim();
    const mode = opened.mode & 0o7777;
    if (content !== "") {
      if (mode !== 0o600) {
        throw new Error(`API key file must have mode 0600, got ${mode.toString(8).padStart(4, "0")}: ${keyPath}`);
      }
      return content;
    }

    // An empty regular file contains no secret yet, so it is safe to repair its mode
    // before generating the first value. The descriptor keeps validation tied to the
    // same inode even if the pathname is concurrently replaced.
    const key = generatedApiKey();
    fchmodSync(fd, 0o600);
    ftruncateSync(fd, 0);
    writeSync(fd, `${key}\n`, 0, "utf8");
    fsyncSync(fd);
    return key;
  } finally {
    closeSync(fd);
  }
}

function createNewKey(keyPath: string): string {
  const key = generatedApiKey();
  // O_EXCL makes pathname creation atomic and O_NOFOLLOW prevents a concurrently
  // inserted symlink from being followed. The secret is never written with a mode
  // broader than 0600.
  const fd = openSync(
    keyPath,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
    0o600,
  );
  try {
    fchmodSync(fd, 0o600);
    writeSync(fd, `${key}\n`, 0, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  return key;
}

/**
 * Return an explicitly configured API key, or securely load/create stateDir/api-key.
 * Unsafe existing paths fail closed; persistence failures are startup errors.
 */
export function ensureApiKey(
  config: ApiKeyConfig,
  effectiveUid: number | undefined = process.geteuid?.(),
  requireAuthentication = false,
): string {
  if (config.apiKey !== "") return config.apiKey;
  if (isLoopbackHost(config.host) && !requireAuthentication) return "";

  mkdirSync(config.stateDir, { recursive: true, mode: 0o700 });
  const keyPath = apiKeyFilePath(config.stateDir);
  try {
    return readOrPopulateExistingKey(keyPath, effectiveUid);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") throw error;
  }

  try {
    return createNewKey(keyPath);
  } catch (error) {
    // A concurrent process may have won O_EXCL. Validate and use its file rather
    // than weakening creation flags or overwriting it.
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      return readOrPopulateExistingKey(keyPath, effectiveUid);
    }
    throw error;
  }
}

function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

export interface StartupBannerOptions {
  baseUrl: string;
  apiKeyEnabled: boolean;
  generatedKeyPath?: string;
}

/** Render operational startup information without ever interpolating a secret. */
export function renderStartupBanner(options: StartupBannerOptions): string {
  const lines = ["", "─".repeat(64), `  端点  ${options.baseUrl}`];
  if (!options.apiKeyEnabled) {
    lines.push("  鉴权  未启用（仅允许回环监听）");
  } else if (options.generatedKeyPath !== undefined) {
    lines.push(
      "  密钥  [已隐藏，不会写入日志]",
      `  文件  ${options.generatedKeyPath}`,
      `  读取  IFS= read -r TRAEX_BRIDGE_API_KEY < ${shellSingleQuote(options.generatedKeyPath)}`,
    );
  } else {
    lines.push("  密钥  [由 TRAEX_BRIDGE_API_KEY 提供，值已隐藏]");
  }
  lines.push("─".repeat(64), "");
  return lines.join("\n");
}
