/**
 * 把客户端断连传播到上游请求。
 *
 * 踩过的坑：`IncomingMessage` 的 `close` **不是**响应生命周期信号 —— 对普通 POST
 * 来说，请求体被消费完它就触发了，远早于模型完成。用它会在第一个 token 之前就
 * 把上游连接掐掉。
 *
 * 正确做法：用 request 的 `aborted` 判断上传中断，用**响应对象**的 `close` /
 * `finish` 判断断连与清理。
 */

import type { FastifyReply, FastifyRequest } from "fastify";

export function buildUpstreamSignal(req: FastifyRequest, reply: FastifyReply): AbortSignal {
  const controller = new AbortController();
  let cleanedUp = false;

  const cleanup = (): void => {
    if (cleanedUp) return;
    cleanedUp = true;
    req.raw.removeListener("aborted", onRequestAborted);
    reply.raw.removeListener("close", onReplyClose);
    reply.raw.removeListener("finish", onReplyFinish);
  };

  const abort = (message: string): void => {
    if (!controller.signal.aborted) controller.abort(new Error(message));
    cleanup();
  };

  function onRequestAborted(): void {
    abort("client disconnected");
  }
  function onReplyClose(): void {
    // 响应还没写完就 close = 客户端断了；写完了只需要清理
    if (!reply.raw.writableFinished) abort("client disconnected");
    else cleanup();
  }
  function onReplyFinish(): void {
    cleanup();
  }

  req.raw.once("aborted", onRequestAborted);
  reply.raw.once("close", onReplyClose);
  reply.raw.once("finish", onReplyFinish);

  // 覆盖「监听器挂上之前 socket 就没了」的竞态
  if (req.raw.destroyed || (reply.raw.destroyed && !reply.raw.writableFinished)) {
    abort("client disconnected");
  }

  return controller.signal;
}
