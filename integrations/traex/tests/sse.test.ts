import { strict as assert } from "node:assert";
import { test } from "node:test";
import { SseBlockDecoder, isTerminalEventName } from "../src/upstream/sse.js";

const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

test("解析基本事件", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc("event: output\ndata: {\"response\":\"hi\"}\n\n"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.event, "output");
  assert.equal(events[0]!.data, '{"response":"hi"}');
});

test("跨 chunk 断行", () => {
  const d = new SseBlockDecoder();
  assert.equal(d.push(enc("event: out")).length, 0);
  assert.equal(d.push(enc("put\ndata: {\"a\":")).length, 0);
  const events = d.push(enc("1}\n\n"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.event, "output");
  assert.equal(events[0]!.data, '{"a":1}');
});

test("CRLF 跨 chunk 撕裂", () => {
  const d = new SseBlockDecoder();
  d.push(enc("data: x\r"));
  const events = d.push(enc("\n\r\n"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "x");
});

test("裸 CR 也终止行", () => {
  const d = new SseBlockDecoder();
  // chunk 末尾的 CR 会被保留（它可能是 CRLF 的前半），要到 finish 才 dispatch
  const mid = d.push(enc("data: a\rdata: b\r\r"));
  assert.equal(mid.length, 0);
  const events = d.finish();
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "a\nb");
});

test("裸 CR 在 chunk 中间时立即终止行", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc("data: a\rdata: b\r\rx"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "a\nb");
});

test("多行 data 用 LF 折叠", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc("data: line1\ndata: line2\n\n"));
  assert.equal(events[0]!.data, "line1\nline2");
});

test("注释行被跳过", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc(": trae-queue {\"position\":9}\ndata: real\n\n"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "real");
});

test("; 心跳行与 : 注释同等对待，不产生字段", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc("; heartbeat\n;data: sneak\ndata: real\n\n"));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "real");
  assert.equal(events[0]!.event, undefined);
});

test("; 与 : 心跳行不计入事件行数上限", () => {
  const d = new SseBlockDecoder();
  const heartbeats = "; hb\n".repeat(70_000) + ": comment\n".repeat(1000);
  const events = d.push(enc(`${heartbeats}data: ok\n\n`));
  assert.equal(events.length, 1);
  assert.equal(events[0]!.data, "ok");
});

test("data: 后只剥一个空格", () => {
  const d = new SseBlockDecoder();
  const events = d.push(enc("data:  two-spaces\n\n"));
  assert.equal(events[0]!.data, " two-spaces");
});

test("EOF 无空行终止时补发尾块", () => {
  const d = new SseBlockDecoder();
  assert.equal(d.push(enc("event: done\ndata: {}")).length, 0);
  const events = d.finish();
  assert.equal(events.length, 1);
  assert.equal(events[0]!.event, "done");
});

test("UTF-8 跨 chunk 撕裂", () => {
  const d = new SseBlockDecoder();
  const full = enc("data: 中文\n\n");
  // 把「中」的三个字节从中间切开
  d.push(full.slice(0, 7));
  const events = d.push(full.slice(7));
  assert.equal(events[0]!.data, "中文");
});

test("超长无换行的行会失败而不是无限缓冲", () => {
  const d = new SseBlockDecoder();
  assert.throws(() => {
    for (let i = 0; i < 20; i++) d.push(enc("x".repeat(1024 * 1024)));
  }, /exceeded/);
});

test("两个终止事件名都认", () => {
  assert.equal(isTerminalEventName("done"), true);
  assert.equal(isTerminalEventName("finish_reason"), true);
  assert.equal(isTerminalEventName("DONE"), true);
  assert.equal(isTerminalEventName("output"), false);
  assert.equal(isTerminalEventName(undefined), false);
});
