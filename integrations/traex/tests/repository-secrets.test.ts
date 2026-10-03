import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import { existsSync, lstatSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FULL_TRAEX_KEY = /tb_[0-9a-f]{48}/gi;

function matchingLines(content: string): number[] {
  return [...content.matchAll(FULL_TRAEX_KEY)].map((match) =>
    content.slice(0, match.index).split("\n").length
  );
}

test("Traex key detector distinguishes full keys from documentation placeholders", () => {
  const fullShape = `tb_${"a".repeat(48)}`;
  assert.deepEqual(matchingLines(fullShape), [1]);
  assert.deepEqual(matchingLines("<自动生成的密钥>\n<密钥前缀>...\ntb_deadbeef..."), []);
});

test("git-tracked text contains no full-shape Traex API key", () => {
  const tracked = execFileSync("git", ["ls-files", "-z"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).split("\0").filter(Boolean);
  const findings: string[] = [];

  for (const relativePath of tracked) {
    const absolutePath = resolve(REPO_ROOT, relativePath);
    if (!existsSync(absolutePath)) continue;
    if (!lstatSync(absolutePath).isFile()) continue;
    const bytes = readFileSync(absolutePath);
    if (bytes.includes(0)) continue;
    for (const line of matchingLines(bytes.toString("utf8"))) {
      findings.push(`${relativePath}:${line}`);
    }
  }

  assert.equal(
    findings.length,
    0,
    `full-shape Traex API key found at: ${findings.join(", ")}`,
  );
});
