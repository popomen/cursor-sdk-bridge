import { strict as assert } from "node:assert";
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { apiKeyFilePath, ensureApiKey, renderStartupBanner } from "../src/api-key.js";

function config(stateDir: string): { apiKey: string; host: string; stateDir: string } {
  return { apiKey: "", host: "0.0.0.0", stateDir };
}

function mode(path: string): number {
  return lstatSync(path).mode & 0o7777;
}

test("new API key is atomically created as a non-empty 0600 regular file", () => {
  const stateDir = join(mkdtempSync(join(tmpdir(), "traex-api-key-new-")), "state");
  const key = ensureApiKey(config(stateDir));
  const keyPath = apiKeyFilePath(stateDir);

  assert.match(key, /^tb_[0-9a-f]{48}$/);
  assert.equal(readFileSync(keyPath, "utf8"), `${key}\n`);
  assert.equal(lstatSync(keyPath).isFile(), true);
  assert.equal(lstatSync(keyPath).isSymbolicLink(), false);
  assert.equal(mode(keyPath), 0o600);
});

test("127.* DNS 名不能借回环豁免跳过 API key", () => {
  const stateDir = join(mkdtempSync(join(tmpdir(), "traex-api-key-dns-host-")), "state");
  const key = ensureApiKey({ apiKey: "", host: "127.example.com", stateDir });
  assert.match(key, /^tb_[0-9a-f]{48}$/);
});

test("existing non-empty API key must have mode exactly 0600", () => {
  const stateDir = mkdtempSync(join(tmpdir(), "traex-api-key-mode-"));
  const keyPath = apiKeyFilePath(stateDir);
  writeFileSync(keyPath, "existing-secret\n", { mode: 0o644 });
  chmodSync(keyPath, 0o644);

  assert.throws(() => ensureApiKey(config(stateDir)), /mode 0600, got 0644/);
  assert.equal(readFileSync(keyPath, "utf8"), "existing-secret\n");
  assert.equal(mode(keyPath), 0o644);
});

test("empty regular file is repaired to 0600 before a key is generated", () => {
  const stateDir = mkdtempSync(join(tmpdir(), "traex-api-key-empty-"));
  const keyPath = apiKeyFilePath(stateDir);
  writeFileSync(keyPath, "", { mode: 0o644 });
  chmodSync(keyPath, 0o644);

  const key = ensureApiKey(config(stateDir));
  assert.match(key, /^tb_[0-9a-f]{48}$/);
  assert.equal(readFileSync(keyPath, "utf8"), `${key}\n`);
  assert.equal(mode(keyPath), 0o600);
});

test("API key symlinks and non-regular paths fail closed", () => {
  const symlinkDir = mkdtempSync(join(tmpdir(), "traex-api-key-link-"));
  const target = join(symlinkDir, "target");
  writeFileSync(target, "secret\n", { mode: 0o600 });
  symlinkSync(target, apiKeyFilePath(symlinkDir));
  assert.throws(() => ensureApiKey(config(symlinkDir)), /regular non-symlink/);

  const directoryDir = mkdtempSync(join(tmpdir(), "traex-api-key-directory-"));
  mkdirSync(apiKeyFilePath(directoryDir));
  assert.throws(() => ensureApiKey(config(directoryDir)), /regular non-symlink/);
});

test("non-root process rejects a foreign owner while root accepts a secure bind mount", () => {
  const stateDir = mkdtempSync(join(tmpdir(), "traex-api-key-owner-"));
  const keyPath = apiKeyFilePath(stateDir);
  writeFileSync(keyPath, "mounted-secret\n", { mode: 0o600 });
  chmodSync(keyPath, 0o600);
  const owner = lstatSync(keyPath).uid;
  const differentUid = owner === 0 ? 1 : owner + 1;

  assert.throws(
    () => ensureApiKey(config(stateDir), differentUid),
    new RegExp(`owned by effective uid ${differentUid}`),
  );
  assert.equal(ensureApiKey(config(stateDir), 0), "mounted-secret");
});

test("startup banner never contains the API key, token exports, or curl commands", () => {
  const secret = `tb_${"f".repeat(48)}`;
  const keyPath = "/var/lib/traex-bridge/api-key";
  const banner = renderStartupBanner({
    baseUrl: "http://127.0.0.1:8317",
    apiKeyEnabled: secret !== "",
    generatedKeyPath: keyPath,
  });

  assert.equal(banner.includes(secret), false);
  assert.doesNotMatch(banner, /tb_[0-9a-f]{48}/i);
  assert.equal(banner.includes("ANTHROPIC_AUTH_TOKEN="), false);
  assert.equal(banner.includes("OPENAI_API_KEY="), false);
  assert.equal(banner.toLowerCase().includes("curl"), false);
  assert.match(banner, /\[已隐藏，不会写入日志\]/);
  assert.match(banner, new RegExp(keyPath.replaceAll("/", "\\/")));
  assert.match(banner, /IFS= read -r TRAEX_BRIDGE_API_KEY/);
});

test("existing-file initialization uses one descriptor without pathname truncation", () => {
  const source = readFileSync(new URL("../src/api-key.ts", import.meta.url), "utf8");
  const existingPath = source.slice(
    source.indexOf("function readOrPopulateExistingKey"),
    source.indexOf("function createNewKey"),
  );
  assert.match(existingPath, /O_RDWR \| constants\.O_NOFOLLOW/);
  assert.match(existingPath, /ftruncateSync\(fd, 0\)/);
  assert.doesNotMatch(existingPath, /constants\.O_TRUNC/);
  assert.equal((existingPath.match(/openSync\(/g) ?? []).length, 1);
  assert.equal(source.includes("chmodSync(keyPath"), false);
});

// The managed bridge requires authentication even though the standalone upstream permits loopback bypass.
test("managed loopback mode creates and reuses a protected API key", () => {
  const stateDir = mkdtempSync(join(tmpdir(), "traex-managed-key-"));
  const input = { apiKey: "", host: "127.0.0.1", stateDir };
  const key = ensureApiKey(input, undefined, true);
  assert.ok(key.length > 0);
  assert.equal(ensureApiKey(input, undefined, true), key);
  assert.equal(mode(apiKeyFilePath(stateDir)), 0o600);
});
