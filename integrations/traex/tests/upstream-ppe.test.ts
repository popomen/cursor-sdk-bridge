import { strict as assert } from "node:assert";
import { createServer, type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import { test } from "node:test";

import { UpstreamClient } from "../src/upstream/client.js";
import type { CredentialProvider } from "../src/upstream/credentials.js";
import {
  buildTraePpeHeaders,
  normalizeTraePpeEnv,
} from "../src/upstream/ppe.js";

test("TRAE_PPE_ENV 对齐 TraeX 的两个上游 header", () => {
  assert.equal(normalizeTraePpeEnv("  example_ppe_environment  "), "example_ppe_environment");
  assert.deepEqual(buildTraePpeHeaders("example_ppe_environment"), {
    "x-use-ppe": "1",
    "x-tt-env": "example_ppe_environment",
  });
  assert.deepEqual(buildTraePpeHeaders(undefined), {});
  assert.deepEqual(buildTraePpeHeaders("   "), {});
  assert.throws(
    () => buildTraePpeHeaders("ppe\nforged-header: value"),
    /TRAE_PPE_ENV must be a valid HTTP header value/,
  );
});

test("PPE header 同时覆盖生成、模型目录和额度请求", async (t) => {
  const requests: Array<{ url: string; headers: IncomingHttpHeaders }> = [];
  const server = createServer((req, res) => {
    req.resume();
    req.once("end", () => {
      requests.push({ url: req.url ?? "", headers: req.headers });
      res.writeHead(200, { "content-type": "application/json" });
      res.end("{}");
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
  });

  const { port } = server.address() as AddressInfo;
  const baseUrl = `http://127.0.0.1:${port}`;
  const credentials = {
    async token(): Promise<string> { return "test-token"; },
    async refresh(): Promise<string> { return "test-token"; },
  } as unknown as CredentialProvider;
  const client = new UpstreamClient({
    baseUrl,
    catalogBaseUrl: baseUrl,
    traePpeEnv: "example_ppe_environment",
    credentials,
    headerTimeoutMs: 5_000,
  });

  const chat = await client.chat({ model: "test" });
  await chat.text();
  await client.getDetailParam();
  await client.getAccountUsage();

  assert.equal(requests.length, 3);
  assert.deepEqual(
    requests.map((request) => request.url),
    [
      "/api/ide/v2/llm_raw_chat",
      "/api/ide/v1/get_detail_param",
      "/trae/api/v1/pay/ide_user_ent_usage",
    ],
  );
  for (const request of requests) {
    assert.equal(request.headers["x-use-ppe"], "1", request.url);
    assert.equal(request.headers["x-tt-env"], "example_ppe_environment", request.url);
  }
});
