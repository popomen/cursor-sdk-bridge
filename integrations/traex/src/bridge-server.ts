/** Entry point used by cursor-sdk-bridge; upstream's standalone server is not launched. */
import { setDefaultResultOrder } from "node:dns";
import { join } from "node:path";
import { ensureApiKey } from "./api-key.js";
import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";
import { ASTRA_MODELS, BridgeRuntime, DEFAULT_MODEL } from "./bridge-runtime.js";
import { CatalogRefresher, CatalogStore, type CatalogModel } from "./upstream/catalog.js";
import { CredentialProvider } from "./upstream/credentials.js";
import { UpstreamClient } from "./upstream/client.js";
import { QuotaCache } from "./upstream/quota.js";
import { UsageStore } from "./upstream/usage.js";

class AstraCatalog extends CatalogStore {
  override set(models: CatalogModel[]): void { super.set(models.filter(m => ASTRA_MODELS.has(m.id))); }
}
// Upstream diagnostics may contain response bodies. Only our allowlisted metadata is logged.
const quiet = { info: (..._args: unknown[]) => {}, warn: (..._args: unknown[]) => {}, error: (..._args: unknown[]) => {} };

let startupPhase = "configuration";
async function main(): Promise<void> {
  setDefaultResultOrder("ipv4first");
  const config = loadConfig();
  if (config.host !== "127.0.0.1") throw new Error("Loopback required");
  config.defaultModel = DEFAULT_MODEL;
  config.logLevel = "silent";
  config.logRequests = config.dumpBody = config.trace = false;
  config.apiKey = ensureApiKey(config, undefined, true);
  const credentials = new CredentialProvider(config);
  const catalog = new AstraCatalog();
  const usage = new UsageStore();
  const client = new UpstreamClient({ baseUrl: config.upstreamBaseUrl, catalogBaseUrl: config.catalogBaseUrl,
    traePpeEnv: config.traePpeEnv, credentials, headerTimeoutMs: config.upstreamHeaderTimeoutMs,
    dumpBody: false, logger: quiet });
  const quota = new QuotaCache(client, catalog, usage, quiet);
  startupPhase = "credentials";
  await credentials.token();
  const refresher = new CatalogRefresher({ client, store: catalog, logger: quiet });
  startupPhase = "catalog";
  await refresher.refreshOnce();
  if (!catalog.lookup(DEFAULT_MODEL)) throw new Error("Astra unavailable");
  const bridge = new BridgeRuntime(process.env["CURSOR_SDK_BRIDGE_RUNNING_VERSION"] ?? "unknown",
    join(process.env["TRAEX_BRIDGE_RUNTIME_DIR"]!, "logs"), config.admission?.maxRequests ?? 8);
  const app = buildApp({ config, client, catalog, quota, usage, credentials, bridge });
  app.server.requestTimeout = 0;
  app.server.headersTimeout = 0;
  app.server.setTimeout(0);
  startupPhase = "listener";
  await app.listen({ host: config.host, port: config.port });
  refresher.start();
  void quota.get().catch(() => {});
  let closing = false;
  const shutdown = async (): Promise<void> => {
    if (closing) return;
    closing = true;
    bridge.draining = true;
    refresher.stop();
    await app.close();
  };
  process.once("SIGTERM", () => void shutdown().catch(() => { process.exitCode = 1; }));
  process.once("SIGINT", () => void shutdown().catch(() => { process.exitCode = 1; }));
}
main().catch(() => { process.stderr.write(JSON.stringify({ event: 'startup_failed', phase: startupPhase }) + '\n'); process.exitCode = 1; });
