/**
 * pipeToRaw 背压死锁的复现测试。
 *
 * 复现的 bug：客户端在 `write()` 返回 false 之后、`drain` 到来之前断开时，socket 被
 * destroy，`drain` 永远不会触发。如果只 `once("drain")`，那个 Promise 永久挂起，
 * 连带 finally 里的 `reader.cancel()` 和 `raw.end()` 都执行不到 —— 上游连接和
 * ReadableStream 一起泄漏。
 *
 * 这个场景光靠 mock 造不出来：得有真实的 TCP 窗口关闭才会让 write 返回 false。
 * 所以这里起真的 HTTP server，客户端连上之后不读（socket 保持 paused，TCP 接收
 * 窗口填满 → 服务端发送缓冲填满 → write 返回 false），再把连接 destroy 掉。
 *
 * 关键是**对照组**：同一个场景同时喂给修复前的写法。如果修复前的那份没有挂起，
 * 说明这个测试压根没走到 bug 路径，那它就是个假的回归测试。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { createServer, type ServerResponse } from "node:http";
import { connect } from "node:net";
import type { AddressInfo } from "node:net";
import type { FastifyReply } from "fastify";

import { pipeToRaw } from "../src/app.js";

type Piper = (stream: ReadableStream<Uint8Array>, reply: FastifyReply) => Promise<void>;

/** 修复前的写法：只等 drain。客户端断开后这里就永远醒不过来。 */
const pipeToRawBeforeFix: Piper = async (stream, reply) => {
  const reader = stream.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (reply.raw.destroyed) break;
      const ok = reply.raw.write(value);
      if (!ok) {
        await new Promise<void>((resolve) => {
          reply.raw.once("drain", () => resolve());
        });
      }
    }
  } catch {
    /* 客户端断开 */
  } finally {
    try { await reader.cancel(); } catch { /* ignore */ }
    if (!reply.raw.writableEnded) reply.raw.end();
  }
};

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

interface ScenarioResult {
  /** pipe 是否在超时前返回 */
  outcome: "resolved" | "hung";
  /** finally 里的 reader.cancel() 有没有跑到 */
  cancelled: boolean;
  /** 确认场景真的把 socket 压到了背压状态，否则整个测试无意义 */
  backpressured: boolean;
}

/**
 * 把一个 piper 放进「客户端不读 + 背压中途断开」的场景里跑。
 */
async function runBackpressureAbort(pipe: Piper, timeoutMs = 3_000): Promise<ScenarioResult> {
  let cancelled = false;
  let res: ServerResponse | undefined;
  let pipePromise: Promise<void> | undefined;
  let resolveStarted: () => void;
  const started = new Promise<void>((r) => { resolveStarted = r; });

  const server = createServer((_req, response) => {
    res = response;
    response.writeHead(200, { "content-type": "text/event-stream" });

    // 64KB 一块，一直供，直到 pipeToRaw 因为背压停下来
    const chunk = new Uint8Array(64 * 1024).fill(0x61);
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(chunk); },
      cancel() { cancelled = true; },
    });

    pipePromise = pipe(stream, { raw: response } as unknown as FastifyReply);
    resolveStarted();
  });

  try {
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const { port } = server.address() as AddressInfo;

    const client = connect(port, "127.0.0.1");
    client.on("error", () => { /* destroy 之后的 ECONNRESET */ });
    await new Promise<void>((r) => client.once("connect", () => r()));

    // 不attach 'data'、显式 pause —— 一个字节都不读，让 TCP 窗口关死
    client.pause();
    client.write("GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n");

    await started;

    // 等到 write() 真的返回 false
    let backpressured = false;
    for (let i = 0; i < 300; i++) {
      if (res?.writableNeedDrain) { backpressured = true; break; }
      await sleep(10);
    }

    // 就在这个窗口里断开：drain 永远不会来了
    client.destroy();

    const outcome = await Promise.race([
      pipePromise!.then(() => "resolved" as const),
      sleep(timeoutMs).then(() => "hung" as const),
    ]);

    return { outcome, cancelled, backpressured };
  } finally {
    res?.destroy();
    await new Promise<void>((r) => server.close(() => r()));
  }
}

test("背压中途断开：修复后的 pipeToRaw 能返回并清理 reader", async () => {
  const r = await runBackpressureAbort(pipeToRaw);

  assert.equal(r.backpressured, true, "场景没能把 socket 压到背压状态，测试本身失效了");
  assert.equal(r.outcome, "resolved", "pipeToRaw 在客户端背压中断开后挂住了");
  assert.equal(r.cancelled, true, "finally 里的 reader.cancel() 没执行到 —— 上游流泄漏");
});

test("对照组：修复前的写法在同一场景下确实会挂住", async () => {
  const r = await runBackpressureAbort(pipeToRawBeforeFix, 1_500);

  assert.equal(r.backpressured, true, "场景没能把 socket 压到背压状态，测试本身失效了");
  // 这条断言是整个文件的地基：它失败就说明上面那条测试是在空跑
  assert.equal(r.outcome, "hung", "修复前的写法没有挂住 —— 说明这个场景根本没触发 bug，上面的测试不成立");
  assert.equal(r.cancelled, false, "既然挂在 await 上，finally 就不该跑到");
});
