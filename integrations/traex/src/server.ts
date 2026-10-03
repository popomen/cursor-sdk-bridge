/**
 * 进程入口：加载配置 → 初始化上游 → 启动 HTTP 服务。
 *
 * 启动顺序敏感，任一步失败即整体启动失败（快速失败，不让配错的东西拖到第一个请求）：
 *   1. 配置校验
 *   2. 凭证 mint 一次
 *   3. catalog 同步拉取（没有编译期 fallback 列表）
 */

import { setDefaultResultOrder } from "node:dns";
import { apiKeyFilePath, ensureApiKey, renderStartupBanner } from "./api-key.js";
import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";

/**
 * 强制 IPv4 优先。
 *
 * 上游 `copilot-cn.bytedance.net` 的 DNS 同时返回 A 和 AAAA 记录，但容器网络
 * 默认没有 IPv6 出口。Node 18+ 默认按 DNS 返回顺序（verbatim）尝试，会先打 IPv6
 * 然后卡满 10 秒的 TCP 连接超时才回退 —— 表现为随机的 UND_ERR_CONNECT_TIMEOUT。
 * 设 `TRAEX_BRIDGE_DNS_RESULT_ORDER=verbatim` 可以恢复默认行为。
 */
if ((process.env["TRAEX_BRIDGE_DNS_RESULT_ORDER"] ?? "ipv4first") === "ipv4first") {
  setDefaultResultOrder("ipv4first");
}
import { CatalogRefresher, CatalogStore } from "./upstream/catalog.js";
import { UpstreamClient } from "./upstream/client.js";
import { CredentialProvider } from "./upstream/credentials.js";
import { QuotaCache } from "./upstream/quota.js";
import { UsageStore } from "./upstream/usage.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const apiKeyWasExplicit = config.apiKey !== "";
  config.apiKey = ensureApiKey(config);

  const credentials = new CredentialProvider(config);
  const catalog = new CatalogStore();
  const usage = new UsageStore();

  const client = new UpstreamClient({
    baseUrl: config.upstreamBaseUrl,
    catalogBaseUrl: config.catalogBaseUrl,
    traePpeEnv: config.traePpeEnv,
    credentials,
    headerTimeoutMs: config.upstreamHeaderTimeoutMs,
    dumpBody: config.dumpBody,
    logger: console,
  });

  const quota = new QuotaCache(client, catalog, usage, console);

  // 1. 凭证：立刻 mint 一次，配错的认证不应该拖到第一个请求才暴露
  try {
    await credentials.token();
  } catch (e) {
    console.error(`[traex-bridge] initial credential load failed (source: ${credentials.describe()})`);
    console.error(`  ${(e as Error).message}`);
    process.exit(1);
  }

  // 2. catalog：必须成功拉一次
  const refresher = new CatalogRefresher({ client, store: catalog, logger: console });
  try {
    await refresher.refreshOnce();
  } catch (e) {
    const err = e as Error & { cause?: { code?: string; message?: string } };
    const cause = err.cause?.code ?? err.cause?.message;
    console.error(
      `[traex-bridge] initial model catalog fetch failed: ${err.message}` +
        (cause ? ` (cause: ${cause})` : ""),
    );
    console.error(`  upstream: ${config.catalogBaseUrl}`);
    process.exit(1);
  }
  refresher.start();

  const app = buildApp({ config, client, catalog, quota, usage, credentials });

  // 预热额度（非阻塞）
  void quota.get().catch(() => {});

  // 流式响应可能跑几分钟 —— 不能有整体写超时
  app.server.requestTimeout = 0;
  app.server.headersTimeout = 0;
  app.server.setTimeout(0);

  try {
    await app.listen({ host: config.host, port: config.port });
  } catch (e) {
    console.error(`[traex-bridge] failed to listen on ${config.host}:${config.port}: ${(e as Error).message}`);
    process.exit(1);
  }

  const base = `http://${config.host === "0.0.0.0" ? "127.0.0.1" : config.host}:${config.port}`;
  app.log.info(
    `traex-bridge ready — ${catalog.size} models, credential source: ${credentials.describe()}, ` +
      `generation mode: stateless, ` +
      `Trae PPE: ${config.traePpeEnv === "" ? "off" : "on"}`,
  );

  console.log(renderStartupBanner({
    baseUrl: base,
    apiKeyEnabled: config.apiKey !== "",
    generatedKeyPath:
      !apiKeyWasExplicit && config.apiKey !== "" ? apiKeyFilePath(config.stateDir) : undefined,
  }));

  // token 过期预警：14 天有效期，静默过期后表现为一堆 401，很难定位
  const warnIfExpiringSoon = (): void => {
    const days = credentials.expiresInDays();
    if (days === null) return;
    if (days <= 0) {
      app.log.error(`Trae 凭证已过期 —— 重新生成 token 文件即可，无需重启容器`);
    } else if (days <= 3) {
      app.log.warn(`Trae 凭证还有 ${days.toFixed(1)} 天过期，记得刷新`);
    }
  };
  warnIfExpiringSoon();
  const expiryTimer = setInterval(warnIfExpiringSoon, 6 * 3600_000);
  expiryTimer.unref?.();

  let shutdownPromise: Promise<void> | null = null;
  const shutdown = (signal: string): Promise<void> => {
    if (shutdownPromise) return shutdownPromise;
    shutdownPromise = (async () => {
      app.log.info(`received ${signal}, shutting down`);
      refresher.stop();
      clearInterval(expiryTimer);
      await app.close();
      process.exit(0);
    })().catch((error: unknown) => {
      app.log.error(`shutdown failed: ${(error as Error).message}`);
      process.exit(1);
    });
    return shutdownPromise;
  };
  process.once("SIGINT", () => void shutdown("SIGINT"));
  process.once("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((e: unknown) => {
  console.error(`[traex-bridge] fatal: ${(e as Error).message}`);
  process.exit(1);
});
