import { strict as assert } from "node:assert";
import { test } from "node:test";

import type { CredentialProvider } from "../src/upstream/credentials.js";
import {
  CredentialPrincipalChangedError,
  UpstreamClient,
} from "../src/upstream/client.js";

function jwt(subject: string, tag: string): string {
  const b64 = (value: unknown): string =>
    Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${b64({ alg: "ES256" })}.${b64({
    aud: "Code",
    iss: "Code",
    sub: subject,
    tag,
    exp: Math.floor(Date.now() / 1000) + 3600,
  })}.sig`;
}

function credentials(initial: string, refreshed: string): CredentialProvider {
  return {
    token: async () => initial,
    refresh: async () => refreshed,
  } as unknown as CredentialProvider;
}

function client(provider: CredentialProvider): UpstreamClient {
  return new UpstreamClient({
    baseUrl: "https://example.invalid",
    catalogBaseUrl: "https://example.invalid",
    credentials: provider,
    headerTimeoutMs: 5_000,
  });
}

test("401 刷新到其他账号时不跨认证主体自动重试", async (t) => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response("", { status: 401 });
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  await assert.rejects(
    client(credentials(jwt("user-a", "old"), jwt("user-b", "new"))).chat({}),
    CredentialPrincipalChangedError,
  );
  assert.equal(calls, 1);
});

test("同账号正常 token 轮换仍可安全重试", async (t) => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response("", { status: calls === 1 ? 401 : 200 });
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const response = await client(
    credentials(jwt("user-a", "old"), jwt("user-a", "new")),
  ).chat({});
  assert.equal(response.status, 200);
  assert.equal(calls, 2);
});

test("admission principal is checked before every attempt and opaque tokens fail closed", async (t) => {
  let token = jwt("user-a", "old");
  const provider = { token: async () => token, refresh: async () => token } as unknown as CredentialProvider;
  const upstream = client(provider);
  const principal = await upstream.principalScope();
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return new Response(""); });
  token = jwt("user-a", "new");
  await upstream.chat({}, undefined, principal);
  token = jwt("user-b", "new");
  await assert.rejects(upstream.chat({}, undefined, principal), CredentialPrincipalChangedError);
  assert.equal(calls, 1);

  token = "opaque-a";
  const opaqueScope = await upstream.principalScope();
  assert.notEqual(opaqueScope, "");
  token = "opaque-b";
  await assert.rejects(upstream.chat({}, undefined, opaqueScope), CredentialPrincipalChangedError);
  assert.equal(calls, 1);
});

test("cancellation while loading admission credentials never reaches fetch", async (t) => {
  const abort = new AbortController();
  const provider = {
    token: async () => { abort.abort(); return jwt("user-a", "x"); },
  } as unknown as CredentialProvider;
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return new Response(""); });
  await assert.rejects(client(provider).chat({}, abort.signal, "p"));
  assert.equal(calls, 0);
});
