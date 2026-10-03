/**
 * `/v1/messages/count_tokens` —— 完全本地估算，不调上游。
 *
 * Trae 上游不提供 tokenizer 接口，所以返回一个保守（偏高）的本地估计。
 * 按 UTF-8 字节数 / 4 计算：中文一个字算 3 字节 → ~0.75 token/字，对中文偏保守。
 */

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const byteLen = (s: string): number => Buffer.byteLength(s, "utf8");

/** 递归统计文本字节数。base64 图片数据会被完整计入 —— 这是刻意的保守。 */
export function countText(v: unknown): number {
  if (v === null || v === undefined) return 0;
  if (typeof v === "string") return byteLen(v);

  if (Array.isArray(v)) {
    let total = 0;
    for (const p of v) {
      if (typeof p === "string") {
        total += byteLen(p);
      } else if (isPlainObject(p)) {
        if (typeof p["text"] === "string") total += byteLen(p["text"]);
        total += countText(p["content"]);
        if (p["input"] !== undefined) total += byteLen(JSON.stringify(p["input"]));
        const source = p["source"];
        if (isPlainObject(source)) {
          if (typeof source["data"] === "string") total += byteLen(source["data"]);
          if (typeof source["url"] === "string") total += byteLen(source["url"]);
        }
      } else {
        total += byteLen(String(p));
      }
    }
    return total;
  }

  if (isPlainObject(v)) {
    let total = 0;
    for (const val of Object.values(v)) total += countText(val);
    return total;
  }

  return byteLen(String(v));
}

export function estimateInputTokens(body: Record<string, unknown>): number {
  let chars = countText(body["system"]);

  const messages = Array.isArray(body["messages"]) ? body["messages"] : [];
  const turns = messages.length;
  for (const m of messages) {
    if (isPlainObject(m)) chars += countText(m["content"]);
  }

  if (body["tools"] !== undefined) {
    chars += byteLen(JSON.stringify(body["tools"]));
  }

  let tokens = Math.ceil(chars / 4);
  tokens += turns * 4; // 每轮消息的固定开销
  return Math.max(tokens, 1);
}
