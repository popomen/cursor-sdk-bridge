#!/usr/bin/env python3
"""Prepare or run official Cursor SDK browser login without printing its key."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

JAVASCRIPT = r'''
import { pathToFileURL } from "node:url";
import { open, unlink } from "node:fs/promises";
const [entry, output] = process.argv.slice(1);
let file;
let saved = false;
try {
  const { Cursor } = await import(pathToFileURL(entry).href);
  file = await open(output, "wx", 0o600);
  const result = await Cursor.auth.login({
    openBrowser: false,
    onLoginUrl: url => console.log("Open this Cursor login URL in your browser:\n" + url),
    apiKeyName: "codex-cursor-fallback-spike",
    store: null,
    signal: AbortSignal.timeout(600000),
  });
  if (typeof result.apiKey !== "string" || !result.apiKey.trim()) {
    throw new Error("Empty SDK key");
  }
  await file.writeFile(result.apiKey.trim() + "\n");
  await file.sync();
  saved = true;
  console.log("SDK user API key saved to " + output + " (mode 0600; key not displayed).");
} catch (error) {
  console.error("SDK login did not finish: " + (error.code === "EEXIST" ? "output already exists" : error.constructor.name));
  process.exitCode = 1;
} finally {
  if (file) {
    await file.close();
    if (!saved) await unlink(output);
  }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="Start browser authorization and create a Cursor user API key")
    parser.add_argument("--output", type=Path, default=Path.home() / ".codex/cursor-sdk-api-key")
    args = parser.parse_args()
    if not args.login:
        print(json.dumps({"action": "preview", "login_started": False,
                          "output": str(args.output), "key_name": "codex-cursor-fallback-spike",
                          "ttl": "SDK default (90 days in 1.0.32)"}))
        return 0
    if args.output.exists():
        parser.error("Output already exists; validate the existing key or choose a new path")
    if not args.output.parent.is_dir():
        parser.error("Output parent directory must already exist")
    module = importlib.util.find_spec("cursor_sdk")
    if module is None:
        parser.error("Use the spike venv with cursor-sdk==1.0.32 installed")
    entry = Path(module.origin).parent / "_vendor/bridge/node_modules/@cursor/sdk/dist/esm/index.js"
    node = shutil.which("node")
    if not node or not entry.is_file():
        parser.error("Node >=22.13 and the bundled TypeScript SDK are required")
    return subprocess.run([node, "--input-type=module", "--eval", JAVASCRIPT,
                           str(entry), str(args.output.resolve())], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
