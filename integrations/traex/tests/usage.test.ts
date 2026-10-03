import { strict as assert } from "node:assert";
import { test } from "node:test";

import { UsageStore } from "../src/upstream/usage.js";

test("缓存读写 token 累加并计算命中率", () => {
  const usage = new UsageStore();
  usage.beginRequest("r1", "m");
  usage.updateRequestUsage("r1", {
    prompt_tokens: 5000,
    completion_tokens: 10,
    total_tokens: 5010,
    reasoning_tokens: 0,
    cache_read_input_tokens: 0,
    cache_write_input_tokens: 5000,
  });
  usage.finishRequest("r1");
  usage.beginRequest("r2", "m");
  usage.updateRequestUsage("r2", {
    prompt_tokens: 5000,
    completion_tokens: 10,
    total_tokens: 5010,
    reasoning_tokens: 0,
    cache_read_input_tokens: 5000,
    cache_write_input_tokens: 0,
  });
  usage.finishRequest("r2");

  const observed = usage.snapshot()[0]!;
  assert.equal(observed.cache_read_input_tokens, 5000);
  assert.equal(observed.cache_write_input_tokens, 5000);
  assert.equal(observed.cache_hit_rate, 0.5);
  assert.equal(observed.requests, 2);
});

test("同一请求的多次累计 usage 只计一个请求并以最后快照为准", () => {
  const usage = new UsageStore();
  usage.beginRequest("r", "m");
  usage.updateRequestUsage("r", {
    prompt_tokens: 10,
    completion_tokens: 2,
    total_tokens: 12,
    reasoning_tokens: 0,
    cache_read_input_tokens: 0,
    cache_write_input_tokens: 10,
  });
  usage.updateRequestUsage("r", {
    prompt_tokens: 20,
    completion_tokens: 5,
    total_tokens: 25,
    reasoning_tokens: 1,
    cache_read_input_tokens: 10,
    cache_write_input_tokens: 0,
  });
  usage.finishRequest("r");

  const observed = usage.snapshot()[0]!;
  assert.equal(observed.requests, 1);
  assert.equal(observed.input_tokens, 20);
  assert.equal(observed.output_tokens, 5);
  assert.equal(observed.total_tokens, 25);
  assert.equal(observed.cache_read_input_tokens, 10);
  assert.equal(observed.cache_write_input_tokens, 0);
});

test("没有 usage 事件的真实请求仍计入 requests", () => {
  const usage = new UsageStore();
  usage.beginRequest("r", "m");
  usage.finishRequest("r");
  const observed = usage.snapshot()[0]!;
  assert.equal(observed.requests, 1);
  assert.equal(observed.total_tokens, 0);
});
