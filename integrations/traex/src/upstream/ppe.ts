/**
 * TraeX 的 PPE 通道路由配置。
 *
 * TraeX 读取 `TRAE_PPE_ENV` 后并不是把环境变量本身传给服务端，而是给每个
 * 上游请求增加 `x-use-ppe: 1` 和 `x-tt-env: <value>`。bridge 在这里复刻同一
 * 语义，让模型目录、额度和生成请求始终来自同一个环境。
 */

export const TRAE_PPE_ENV_NAME = "TRAE_PPE_ENV";

/** 去掉 TraeX 同样会忽略的首尾空白，并在启动阶段拒绝非法 HTTP header 值。 */
export function normalizeTraePpeEnv(raw: string | undefined): string {
  const value = (raw ?? "").trim();
  if (value === "") return "";

  try {
    // 使用运行时自己的校验规则，避免配置看似生效、到第一次请求才被 fetch 拒绝。
    new Headers({ "x-tt-env": value });
  } catch (cause) {
    throw new Error(`${TRAE_PPE_ENV_NAME} must be a valid HTTP header value`, { cause });
  }
  return value;
}

export function buildTraePpeHeaders(raw: string | undefined): Record<string, string> {
  const value = normalizeTraePpeEnv(raw);
  if (value === "") return {};
  return {
    "x-use-ppe": "1",
    "x-tt-env": value,
  };
}
