/**
 * 凭证轮换行为测试。
 *
 * 这是整个 SSH 自换方案的核心承诺——「过期前自动换新，用户不用管」——但它
 * 在真实环境里 14 天才发生一次，没法靠部署验证。之前那批 git 测试全是解析层
 * （从 git 输出里抠 JWT），一条都没覆盖轮换本身。
 *
 * 这里用 command 来源代替 git 来源：两者在 CredentialProvider 眼里走的是同一条
 * 路径（跑个命令、拿 stdout、解析、按 exp 缓存），区别只在命令是什么。让那个
 * 命令每次吐不同有效期的 JWT，就能把 14 天压缩成几毫秒。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { CredentialProvider, tokenPrincipalScope } from "../src/upstream/credentials.js";
import type { Config } from "../src/config.js";

/** 造一个 payload 里 exp 为指定时刻的 JWT（不验签，只解析 payload） */
function jwt(expMs: number, tag: string): string {
  const b64 = (o: unknown): string =>
    Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "ES256", typ: "JWT" })}.${b64({
    aud: "Code",
    iss: "Code",
    sub: "tester",
    tag,
    exp: Math.floor(expMs / 1000),
  })}.sig`;
}

const DAY = 86_400_000;

/**
 * 造一个每次调用都吐下一个 token 的「凭证命令」，并记录被调用了几次。
 * 调用次数写在文件里 —— 命令是子进程，没法直接共享内存。
 */
function scriptedProvider(tokens: string[]): { provider: CredentialProvider; calls: () => number } {
  const dir = mkdtempSync(join(tmpdir(), "traex-rot-"));
  const counter = join(dir, "n");
  writeFileSync(counter, "0");
  const list = join(dir, "tokens");
  writeFileSync(list, tokens.join("\n"));

  // 读计数 → 自增 → 吐出对应那一行；超出就一直吐最后一个
  const cmd =
    `n=$(cat ${counter}); echo $((n+1)) > ${counter}; ` +
    `sed -n "$((n+1))p" ${list} | grep . || tail -1 ${list}`;

  const cfg = {
    credentialSource: "command",
    traeToken: "",
    traeTokenFile: "",
    traeTokenCommand: cmd,
    bytedcliBin: "",
    tokenCommandTimeoutMs: 10_000,
  } as unknown as Config;

  return {
    provider: new CredentialProvider(cfg),
    calls: () => Number(readFileSync(counter, "utf8").trim()),
  };
}

test("凭证还早时复用缓存，不重复去换", async () => {
  const { provider, calls } = scriptedProvider([jwt(Date.now() + 14 * DAY, "A")]);

  const a = await provider.token();
  const b = await provider.token();
  const c = await provider.token();

  assert.equal(a, b);
  assert.equal(b, c);
  assert.equal(calls(), 1, `换了 ${calls()} 次，应该只换 1 次`);
});

test("凭证临近过期时自动换新（14 天轮换的核心）", async () => {
  // 第一个 30 秒后过期 —— 落在 REFRESH_SKEW_MS(60s) 之内，应当立刻被判定为要换
  const soon = jwt(Date.now() + 30_000, "SOON");
  const fresh = jwt(Date.now() + 14 * DAY, "FRESH");
  const { provider, calls } = scriptedProvider([soon, fresh]);

  const first = await provider.token();
  assert.equal(first, soon, "第一次应该拿到那个即将过期的");

  const second = await provider.token();
  assert.equal(second, fresh, "临近过期却没有去换新的 —— 14 天后服务会带着废票一直跑");
  assert.equal(calls(), 2);

  // 换到手的是长期票，就不该再换了
  const third = await provider.token();
  assert.equal(third, fresh);
  assert.equal(calls(), 2, "拿到长期凭证后又多换了一次");
});

test("已经过期的凭证一定不会被继续使用", async () => {
  const dead = jwt(Date.now() - DAY, "DEAD");
  const alive = jwt(Date.now() + 14 * DAY, "ALIVE");
  const { provider } = scriptedProvider([dead, alive]);

  await provider.token();               // 拿到 dead
  const next = await provider.token();  // 必须换掉

  assert.equal(next, alive, "过期凭证被继续使用了");
});

test("上游 401 时强制换新，即使缓存还没到期", async () => {
  const rejected = jwt(Date.now() + 14 * DAY, "REJECTED");
  const renewed = jwt(Date.now() + 14 * DAY, "RENEWED");
  const { provider, calls } = scriptedProvider([rejected, renewed]);

  const t = await provider.token();
  assert.equal(t, rejected);

  // 上游说这张票不认 —— 哪怕本地看它还有 14 天，也得换
  const after = await provider.refresh(rejected);
  assert.equal(after, renewed, "401 之后没换新票，重试必然再次 401");
  assert.equal(calls(), 2);
});

test("并发 401 只换一次，不打成刷新风暴", async () => {
  const rejected = jwt(Date.now() + 14 * DAY, "REJECTED");
  const renewed = jwt(Date.now() + 14 * DAY, "RENEWED");
  const { provider, calls } = scriptedProvider([rejected, renewed]);

  const t = await provider.token();

  // 同时有 5 个在飞的请求都撞上 401
  const results = await Promise.all(Array.from({ length: 5 }, () => provider.refresh(t)));

  assert.ok(results.every((r) => r === renewed), "并发刷新拿到了不一致的凭证");
  assert.equal(calls(), 2, `换了 ${calls()} 次 —— 每个 401 都去换一次会打爆签发方`);
});

test("并发首次取用共享同一次换取（单飞）", async () => {
  const { provider, calls } = scriptedProvider([jwt(Date.now() + 14 * DAY, "ONLY")]);

  const all = await Promise.all(Array.from({ length: 8 }, () => provider.token()));

  assert.equal(new Set(all).size, 1);
  assert.equal(calls(), 1, `冷启动并发换了 ${calls()} 次，应该合并成 1 次`);
});

test("expiresInDays 反映的是真实剩余天数", async () => {
  const { provider } = scriptedProvider([jwt(Date.now() + 14 * DAY, "A")]);
  await provider.token();

  const d = provider.expiresInDays();
  assert.ok(d !== null && d > 13.9 && d <= 14.01, `剩余天数算错了: ${d}`);
});

test("principal scope 对正常 token 轮换稳定，对账号切换敏感", async () => {
  const now = Date.now();
  const first = jwt(now + 7 * DAY, "A");
  const rotated = jwt(now + 14 * DAY, "B");
  const otherPayload = (sub: string): string => {
    const b64 = (o: unknown): string => Buffer.from(JSON.stringify(o)).toString("base64url");
    return `${b64({ alg: "ES256" })}.${b64({
      aud: "Code",
      iss: "Code",
      sub,
      exp: Math.floor((now + 14 * DAY) / 1000),
    })}.sig`;
  };

  assert.equal(tokenPrincipalScope(first), tokenPrincipalScope(rotated));
  assert.notEqual(tokenPrincipalScope(first), tokenPrincipalScope(otherPayload("other-user")));
  assert.equal(tokenPrincipalScope("opaque-token"), "");
});
