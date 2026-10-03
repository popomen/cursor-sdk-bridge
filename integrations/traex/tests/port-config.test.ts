import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  isLoopbackHost,
  loadMaxRequestBytes,
  parseListenAddress,
  TRAE_UPSTREAM_MAX_REQUEST_BYTES,
} from "../src/config.js";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath: string): string {
  return readFileSync(resolve(REPO_ROOT, relativePath), "utf8");
}

test("Docker publish variables never change the application listen address", () => {
  assert.deepEqual(
    parseListenAddress({
      TRAEX_BRIDGE_PUBLISH_HOST: "0.0.0.0",
      TRAEX_BRIDGE_PUBLISH_PORT: "19090",
    }),
    { host: "127.0.0.1", port: 8317 },
  );
});

test("application listen overrides remain independent from Docker publishing", () => {
  assert.deepEqual(
    parseListenAddress({
      TRAEX_BRIDGE_ADDR: "0.0.0.0:9001",
      TRAEX_BRIDGE_PUBLISH_HOST: "127.0.0.1",
      TRAEX_BRIDGE_PUBLISH_PORT: "19090",
    }),
    { host: "0.0.0.0", port: 9001 },
  );

  assert.deepEqual(
    parseListenAddress({ TRAEX_BRIDGE_HOST: "::1", TRAEX_BRIDGE_PORT: "9002" }),
    { host: "::1", port: 9002 },
  );
});

test("loopback 判定只接受字面回环地址，不把 127.* DNS 名当回环", () => {
  assert.equal(isLoopbackHost("127.0.0.1"), true);
  assert.equal(isLoopbackHost("127.255.1.2"), true);
  assert.equal(isLoopbackHost("::1"), true);
  assert.equal(isLoopbackHost("[::1]"), true);
  assert.equal(isLoopbackHost("localhost"), true);
  assert.equal(isLoopbackHost("127.example.com"), false);
  assert.equal(isLoopbackHost("127.0.0.1.example.com"), false);
  assert.equal(isLoopbackHost("::ffff:127.0.0.1"), false);
});

test("request body default and zero compatibility match the TraeX 1 GiB ingress limit", () => {
  assert.equal(loadMaxRequestBytes({}), TRAE_UPSTREAM_MAX_REQUEST_BYTES);
  assert.equal(
    loadMaxRequestBytes({ TRAEX_BRIDGE_MAX_REQUEST_BYTES: "0" }),
    TRAE_UPSTREAM_MAX_REQUEST_BYTES,
  );
  assert.equal(
    loadMaxRequestBytes({ TRAEX_BRIDGE_MAX_REQUEST_BYTES: "123456" }),
    123456,
  );
  assert.throws(() => loadMaxRequestBytes({ TRAEX_BRIDGE_MAX_REQUEST_BYTES: "-1" }));
  assert.throws(() => loadMaxRequestBytes({ TRAEX_BRIDGE_MAX_REQUEST_BYTES: "1.5" }));
  assert.throws(() => loadMaxRequestBytes({ TRAEX_BRIDGE_MAX_REQUEST_BYTES: "1073741825" }));
});
