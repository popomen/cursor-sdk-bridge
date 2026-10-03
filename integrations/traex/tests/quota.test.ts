import { strict as assert } from "node:assert";
import { test } from "node:test";

import { parseAccountQuota } from "../src/upstream/quota.js";

test("额度解析选择 Free 包并兼容 data、camelCase 与三层 quota", () => {
  const multiPack = parseAccountQuota({
    is_dollar_usage_billing: true,
    user_entitlement_pack_list: [
      {
        entitlement_base_info: { product_type: 1, entitlement_id: "paid" },
        quota: { basic_usage_limit: 999 },
        usage: { basic_usage_amount: 1 },
      },
      {
        entitlement_base_info: {
          product_type: "free",
          entitlement_id: "free-pack",
          end_time: 12345,
          quota: { basic_usage_limit: 999 },
        },
        quota: {
          basic_usage_limit: 10,
          advanced_model_request_limit: 20,
          premium_model_fast_request_limit: -1,
          premium_model_slow_request_limit: 4,
          auto_completion_limit: 5,
          bonus_usage_limit: 6,
        },
        usage: {
          basic_usage_amount: 0.44,
          advanced_model_request_usage: 2,
          premium_model_fast_request_usage: 3,
          premium_model_slow_request_usage: 4,
          auto_completion_usage: 1,
          bonus_usage_amount: 8,
        },
      },
    ],
  });
  assert.equal(multiPack.pack, "free-pack");
  assert.equal(multiPack.resets_at, 12345);
  assert.equal(multiPack.dollar_billing, true);
  assert.deepEqual(multiPack.pools.map((pool) => pool.name), [
    "basic", "advanced", "premium_fast", "premium_slow", "auto_completion", "bonus",
  ]);
  assert.deepEqual(multiPack.pools.find((pool) => pool.name === "basic"), {
    name: "basic", limit: 10, used: 0.44, remaining: 9.56, percent: 4, depleted: false, unlimited: false,
  });
  assert.equal(multiPack.pools.find((pool) => pool.name === "premium_fast")?.unlimited, true);
  assert.equal(multiPack.pools.find((pool) => pool.name === "bonus")?.percent, 100);

  const dataWrapped = parseAccountQuota({
    data: {
      is_dollar_usage_billing: true,
      user_entitlement_pack_list: [{
        entitlement_base_info: {
          product_type: 0,
          entitlement_id: "data-pack",
          end_time: 67890,
          quota: { basic_usage_limit: 25 },
        },
        usage: { basic_usage_amount: 10 },
      }],
    },
  });
  assert.equal(dataWrapped.pack, "data-pack");
  assert.equal(dataWrapped.dollar_billing, true);
  assert.equal(dataWrapped.pools[0]?.limit, 25);
  assert.equal(dataWrapped.pools[0]?.percent, 40);

  const camelCase = parseAccountQuota({
    isDollarUsageBilling: true,
    data: {
      userEntitlementPackList: [{
        entitlementBaseInfo: {
          productType: "FREE",
          entitlementId: "camel-pack",
          endTime: 24680,
          productExtra: { subscriptionExtra: { quota: { basicUsageLimit: 80 } } },
        },
        usage: { basicUsageAmount: 81 },
      }],
    },
  });
  assert.equal(camelCase.pack, "camel-pack");
  assert.equal(camelCase.resets_at, 24680);
  assert.equal(camelCase.dollar_billing, true);
  assert.deepEqual(camelCase.pools[0], {
    name: "basic", limit: 80, used: 81, remaining: 0, percent: 100, depleted: true, unlimited: false,
  });

  assert.throws(
    () => parseAccountQuota({ user_entitlement_pack_list: [{ entitlement_base_info: { product_type: 1 } }] }),
    /free entitlement pack not found/,
  );
});
