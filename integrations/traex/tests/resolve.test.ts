/**
 * 模型解析规则链。核心不对称：精确 id 不做 max 偏好，alias / default 才做。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { CatalogStore, parseCatalog, parseReasoningEfforts, variantId } from "../src/upstream/catalog.js";
import { aliasFor, lookupModelId, resolveModel } from "../src/models/resolve.js";

function fixture(): CatalogStore {
  const store = new CatalogStore();
  const mk = (id: string, config: string, model: string) => ({
    id, config, model,
    contextWindow: model.endsWith("__max") ? 936000 : 168000,
    usageLimited: config.startsWith("openrouter"),
    repoRestricted: false, superModel: false, feeLevel: 2, manualUsage: 1,
  });
  store.set([
    mk("GPT-5.5", "gpt-5.5", "gpt-5.5__dev"),
    mk("GPT-5.5[1m]", "gpt-5.5", "gpt-5.5__max"),
    mk("GPT-5.6-Sol", "gpt-5.6-sol", "gpt-5.6-sol__dev"),
    mk("GPT-5.6-Sol[1m]", "gpt-5.6-sol", "gpt-5.6-sol__max"),
    mk("GPT-6-Astra", "gpt-6-astra", "gpt-6-astra__dev"),
    mk("GPT-6-Astra[1m]", "gpt-6-astra", "gpt-6-astra__max"),
    mk("openrouter-3o", "openrouter-3o", "openrouter-3o__dev"),
    mk("openrouter-3o[1m]", "openrouter-3o", "openrouter-3o__max"),
    mk("openrouter-2o", "openrouter-2o", "openrouter-2o__dev"),
    mk("openrouter-1o", "openrouter-1o", "openrouter-1o__dev"),
    mk("Gemini-3.1-Pro-Preview", "gemini-3.1-pro", "gemini-3.1-pro__dev"),
  ]);
  return store;
}

const DEFAULT = "openrouter-3o";
const resolve = (input: string, def = DEFAULT): string => resolveModel(fixture(), input, def).model;

test("variantId: __max 暴露成 [1m]，__dev 保留 base", () => {
  assert.equal(variantId("openrouter-3o", "openrouter-3o__dev"), "openrouter-3o");
  assert.equal(variantId("openrouter-3o", "openrouter-3o__max"), "openrouter-3o[1m]");
  assert.equal(variantId("GPT-5.6-Sol", "gpt-5.6-sol__max"), "GPT-5.6-Sol[1m]");
  assert.equal(variantId("foo", "foo__turbo"), "foo-turbo");
  assert.equal(variantId("foo", "foo"), "foo");
});

test("精确 catalog id 原样生效，不升级到 max", () => {
  assert.equal(resolve("GPT-5.5"), "gpt-5.5__dev");
  assert.equal(resolve("GPT-5.5[1m]"), "gpt-5.5__max");
  // 显式发标准档 = 钉住小 context 变体的逃生阀
  assert.equal(resolve("openrouter-3o"), "openrouter-3o__dev");
  assert.equal(resolve("openrouter-3o[1m]"), "openrouter-3o__max");
});

test("精确查找大小写不敏感", () => {
  assert.equal(resolve("gpt-5.5[1m]"), "gpt-5.5__max");
  assert.equal(resolve("gpt-5.6-sol"), "gpt-5.6-sol__dev");
});

test("-max / __max 后缀重写到 [1m]", () => {
  assert.equal(resolve("gpt-5.5-max"), "gpt-5.5__max");
  assert.equal(resolve("gpt-5.5__max"), "gpt-5.5__max");
  assert.equal(resolve("gpt-5.6-sol-max"), "gpt-5.6-sol__max");
  assert.equal(resolve("openrouter-1o__max"), "openrouter-1o__dev", "没有 max 变体时应保持 base");
});

test("没有 max 变体时保持同家族 base，不误切到相似模型", () => {
  const store = new CatalogStore();
  const mk = (id: string, config: string, model: string) => ({
    id, config, model, contextWindow: 168000, usageLimited: false,
    repoRestricted: false, superModel: false, feeLevel: 0, manualUsage: 0,
  });
  store.set([
    mk("DeepSeek-V4-Flash", "deepseek-v4-flash", "deepseek-v4-flash__dev"),
    mk("DeepSeek-V4-Pro", "deepseek-v4-pro", "deepseek-v4-pro__dev"),
    mk("Gemini-3-Flash-Preview", "gemini-3-flash-preview", "gemini-3-flash-preview__dev"),
    mk("Gemini-3.1-Pro-Preview", "gemini-3.1-pro-preview", "gemini-3.1-pro-preview__dev"),
    mk("GPT-5.2", "gpt-5.2", "gpt-5.2__dev"),
    mk("GPT-5.5[1m]", "gpt-5.5", "gpt-5.5__max"),
  ]);

  assert.equal(resolveModel(store, "DeepSeek-V4-Flash__max", DEFAULT).model, "deepseek-v4-flash__dev");
  assert.equal(resolveModel(store, "Gemini-3-Flash-Preview__max", DEFAULT).model, "gemini-3-flash-preview__dev");
  assert.equal(resolveModel(store, "GPT-5.2__max", DEFAULT).model, "gpt-5.2__dev");
});

test("family alias 会偏好 max 变体", () => {
  assert.equal(resolve("claude-3-opus-20240229"), "openrouter-3o__max");
  assert.equal(resolve("claude-3-opus-max"), "openrouter-3o__max");
  assert.equal(resolve("gpt-4o"), "gpt-5.5__max");
  assert.equal(resolve("astra"), "gpt-6-astra__max");
  assert.equal(resolve("gpt-6"), "gpt-6-astra__max");
  assert.equal(resolve("gpt6"), "gpt-6-astra__max");
});

test("family 没有 max 变体时保持标准档", () => {
  assert.equal(resolve("claude-sonnet-4-5"), "openrouter-2o__dev");
  assert.equal(resolve("claude-3-5-haiku-latest"), "openrouter-1o__dev");
  assert.equal(resolve("claude-3-5-haiku-max"), "openrouter-1o__dev");
  assert.equal(resolve("gemini-2.0-flash"), "gemini-3.1-pro__dev");
});

test("bedrock 风格模型名也能 alias", () => {
  assert.equal(resolve("us.anthropic.claude-sonnet-4"), "openrouter-2o__dev");
});

test("alias 顺序：opus/sonnet/haiku 必须先于 claude", () => {
  // 否则 claude-3-opus 会被泛匹配成 sonnet 档
  assert.equal(aliasFor("claude-3-opus-20240229"), "openrouter-3o");
  assert.equal(aliasFor("claude-sonnet-4-5"), "openrouter-2o");
  assert.equal(aliasFor("claude-3-5-haiku"), "openrouter-1o");
  assert.equal(aliasFor("claude-x"), "openrouter-2o");
  // astra/gpt-6 必须先于 gpt，否则 gpt-6 会被泛匹配成 gpt-5.5
  assert.equal(aliasFor("astra"), "GPT-6-Astra");
  assert.equal(aliasFor("gpt-6"), "GPT-6-Astra");
  assert.equal(aliasFor("gpt6"), "GPT-6-Astra");
  assert.equal(aliasFor("gpt-6-astra"), "GPT-6-Astra");
  assert.equal(aliasFor("gpt-5.5"), "GPT-5.5");
  assert.equal(aliasFor("gpt-4o"), "GPT-5.5");
});

test("未知模型落到 default 并偏好 max", () => {
  assert.equal(resolve("totally-bogus-xyz"), "openrouter-3o__max");
  assert.equal(resolve(""), "openrouter-3o__max");
});

test("default 自身是不是 max 变体不影响精确 id 的行为", () => {
  assert.equal(resolve("GPT-5.5", "openrouter-3o[1m]"), "gpt-5.5__dev");
  assert.equal(resolve("bogus", "openrouter-3o[1m]"), "openrouter-3o__max");
});

test("default 不在 catalog 时原样当作 config/model 发上游", () => {
  const r = resolveModel(fixture(), "bogus", "some-private-model");
  assert.equal(r.config, "some-private-model");
  assert.equal(r.model, "some-private-model");
  assert.equal(r.entry, undefined);
});

test("lookupModelId: a__b 会试 a-b", () => {
  const store = new CatalogStore();
  store.set([
    { id: "foo-bar", config: "c", model: "m", contextWindow: 1, usageLimited: false, repoRestricted: false, superModel: false, feeLevel: 0, manualUsage: 0 },
  ]);
  assert.equal(lookupModelId(store, "foo__bar")?.id, "foo-bar");
});

test("parseCatalog: 只保留可见、启用的 chat_completion config", () => {
  const models = parseCatalog({
    config_info_list: [
      { config_name: "wrong-usage", usage: "completion", config_switch: true, model_detail_list: [{ model_name: "wrong-usage__dev" }] },
      { config_name: "invisible", usage: "chat_completion", is_invisible_to_user: true, config_switch: true, model_detail_list: [{ model_name: "invisible__dev" }] },
      { config_name: "off", usage: "chat_completion", config_switch: false, model_detail_list: [{ model_name: "off__dev" }] },
      { config_name: "missing-switch", usage: "chat_completion", model_detail_list: [{ model_name: "missing-switch__dev" }] },
      { config_name: "on", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "on__dev", prompt_max_tokens: 95 }] },
    ],
  });
  assert.equal(models.length, 1);
  assert.equal(models[0]!.id, "on");
  assert.equal(models[0]!.contextWindow, 100);
});

test("parseCatalog: 上下文窗口按 input/output 组合并只封顶内置 Codex GPT", () => {
  const models = parseCatalog({
    config_info_list: [
      { config_name: "gpt-5.6-sol", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "gpt-5.6-sol__dev", prompt_max_tokens: 240_000, max_tokens: 32_000 }] },
      { config_name: "gpt-5.6-terra", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "model_hub/gpt-5.6-terra__max", prompt_max_tokens: 936_000, max_tokens: 64_000 }] },
      { config_name: "prompt-only", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "prompt-only__dev", prompt_max_tokens: 95_001 }] },
      { config_name: "output-only", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "output-only__dev", max_tokens: 4_000 }] },
      { config_name: "fallback", usage: "chat_completion", config_switch: true, model_detail_list: [{ model_name: "fallback__dev" }] },
      { config_name: "custom-gpt-5.6-sol", usage: "chat_completion", config_source: 2, config_switch: true, model_detail_list: [{ model_name: "custom-gpt-5.6-sol__dev", prompt_max_tokens: 900_000, max_tokens: 100_000 }] },
      { config_name: "gpt-5.6-luna", usage: "chat_completion", config_switch: true, display_config: { is_custom_model: true }, model_detail_list: [{ model_name: "gpt-5.6-luna__dev", prompt_max_tokens: 900_000, max_tokens: 100_000 }] },
    ],
  });
  const windows = Object.fromEntries(models.map((model) => [model.config, model.contextWindow]));
  assert.equal(windows["gpt-5.6-sol"], 272_000);
  assert.equal(windows["gpt-5.6-terra"], 800_000);
  assert.equal(windows["prompt-only"], 100_001);
  assert.equal(windows["output-only"], 132_000);
  assert.equal(windows["fallback"], 128_000);
  assert.equal(windows["custom-gpt-5.6-sol"], 1_000_000);
  assert.equal(windows["gpt-5.6-luna"], 1_000_000);
});

test("parseCatalog: 负载从 config 级继承，保留 0 并忽略非法值", () => {
  const models = parseCatalog({
    config_info_list: [
      {
        config_name: "loaded",
        usage: "chat_completion",
        config_switch: true,
        display_config: { hot_info: { hot: 0 } },
        model_detail_list: [{ model_name: "loaded__dev" }, { model_name: "loaded__max" }],
      },
      {
        config_name: "invalid",
        usage: "chat_completion",
        config_switch: true,
        display_config: { hot_info: { hot: "42" } },
        model_detail_list: [{ model_name: "invalid__dev" }],
      },
    ],
  });
  assert.deepEqual(models.filter((model) => model.config === "loaded").map((model) => model.load), [0, 0]);
  assert.equal(models.find((model) => model.config === "invalid")?.load, undefined);
});

test("parseCatalog: 计费 flag 从 config 级继承给所有变体", () => {
  const models = parseCatalog({
    config_info_list: [
      {
        config_name: "x",
        usage: "chat_completion",
        config_switch: true,
        display_config: { display_name: "X", is_internal_usage_limit: true, is_l4_repo_restricted: true, fee_model_level: 3 },
        model_detail_list: [{ model_name: "x__dev" }, { model_name: "x__max" }],
      },
    ],
  });
  assert.equal(models.length, 2);
  for (const m of models) {
    assert.equal(m.usageLimited, true);
    assert.equal(m.repoRestricted, true);
    assert.equal(m.feeLevel, 3);
  }
  assert.deepEqual(models.map((m) => m.id), ["X", "X[1m]"]);
});

test("parseCatalog: 空 config_info_list 报错（没有编译期 fallback 列表）", () => {
  assert.throws(() => parseCatalog({ config_info_list: [] }), /empty config_info_list/);
});

test("model_extra_config 是被编码过的字符串，要二次 parse", () => {
  const r = parseReasoningEfforts('{"ReasoningEffort":"xhigh","reasoning_effort_options":["max","high","low"]}');
  assert.equal(r?.defaultEffort, "low");
  assert.deepEqual(r?.supportedEfforts, ["low", "high", "max"]);
});

test("reasoning 默认兼容三种字段，options 去重排序并修复不支持的默认值", () => {
  const snake = parseReasoningEfforts('{"reasoning_effort":"HIGH","reasoning_effort_options":["xhigh","low","HIGH","low","invalid"]}');
  assert.deepEqual(snake, { supportedEfforts: ["low", "high", "xhigh"], defaultEffort: "high" });

  const thinking = parseReasoningEfforts('{"Thinking":{"Type":"ultra"},"reasoning_effort_options":["max","minimal"]}');
  assert.deepEqual(thinking, { supportedEfforts: ["minimal", "max"], defaultEffort: "minimal" });

  const secondCandidate = parseReasoningEfforts('{"ReasoningEffort":"ultra","Thinking":{"Type":"high"},"reasoning_effort_options":["medium","high"]}');
  assert.deepEqual(secondCandidate, { supportedEfforts: ["medium", "high"], defaultEffort: "high" });

  const noOptions = parseReasoningEfforts('{"thinking":{"type":"XHIGH"}}');
  assert.deepEqual(noOptions, { supportedEfforts: [], defaultEffort: "xhigh" });
});

test("非推理模型的 model_extra_config 缺失/非法都不抛异常", () => {
  assert.equal(parseReasoningEfforts(undefined), undefined);
  assert.equal(parseReasoningEfforts(""), undefined);
  assert.equal(parseReasoningEfforts("not json"), undefined);
  assert.equal(parseReasoningEfforts("{}"), undefined);
});

test("preferMax 只在有 __max 变体时生效", () => {
  const store = fixture();
  const sonnet = store.lookup("openrouter-2o")!;
  assert.equal(store.preferMax(sonnet).model, "openrouter-2o__dev");
  const opus = store.lookup("openrouter-3o")!;
  assert.equal(store.preferMax(opus).model, "openrouter-3o__max");
});
