/**
 * 工具相关的公共逻辑：JSON Schema 清洗 + 工具调用完整性校验。
 *
 * 这两件事三条下游协议都要做，但上游是同一个。之前只有 Responses 路径做了，
 * Anthropic / Chat 漏了 —— 表现为「Claude Code 挂某个 MCP server 就 400，
 * 换成 Codex 就正常」这种极难定位的现象。放在公共模块里，加新协议时不会再漏。
 */

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 普通对象上的安全赋值：JSON Schema 允许属性名叫 `__proto__`。 */
function setOwn(out: Record<string, unknown>, key: string, value: unknown): void {
  Object.defineProperty(out, key, {
    value,
    enumerable: true,
    configurable: true,
    writable: true,
  });
}

// ---------------------------------------------------------------- schema 清洗

/**
 * 这些键的 value 是「名字 → schema」的 map，要逐个 value 递归而不是当 schema 处理。
 */
const SCHEMA_MAP_KEYS = new Set(["properties", "$defs", "definitions"]);

/** 上游接受的 JSON Schema 关键字白名单 */
const SCHEMA_KEYS = new Set([
  "type", "description", "required", "additionalProperties", "items", "enum",
  "oneOf", "anyOf", "allOf", "default", "nullable",
  "minimum", "maximum", "minLength", "maxLength", "pattern",
]);

/** 防御畸形/恶意输入：schema 嵌套不可能有这么深 */
const MAX_SCHEMA_DEPTH = 64;

/**
 * JSON Schema 白名单清洗。
 *
 * 上游对 schema 挑剔：`$ref`、`format`、`$schema`、客户端私有扩展（如 Codex 的
 * `encrypted`）都可能让它 400。只保留已知安全的关键字。
 *
 * properties / $defs / definitions 的 value 有独立 map 分支，不需要用“零命中时
 * 原样返回”猜上下文。那个逃生阀会让纯 `$ref` / `format` 节点完整穿透，恰好绕过
 * 本函数的安全目标。
 */
export function sanitizeJsonSchema(x: unknown, depth = 0): unknown {
  if (depth > MAX_SCHEMA_DEPTH) return {};
  if (Array.isArray(x)) return x.map((v) => sanitizeJsonSchema(v, depth + 1));
  if (!isPlainObject(x)) return x;

  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(x)) {
    if (SCHEMA_MAP_KEYS.has(k)) {
      if (isPlainObject(v)) {
        const inner: Record<string, unknown> = {};
        for (const [name, schema] of Object.entries(v)) {
          setOwn(inner, name, sanitizeJsonSchema(schema, depth + 1));
        }
        setOwn(out, k, inner);
      }
    } else if (SCHEMA_KEYS.has(k)) {
      setOwn(out, k, sanitizeJsonSchema(v, depth + 1));
    }
  }
  return out;
}

/** 工具的 parameters 缺失时的默认值 */
export const EMPTY_TOOL_PARAMETERS = { type: "object", properties: {}, additionalProperties: false } as const;

/** 工具执行返回空内容时的占位符——让模型能区分「没输出」和「输出丢了」 */
export const EMPTY_TOOL_OUTPUT_PLACEHOLDER = "[tool completed with no output]";

// ------------------------------------------------------- 工具调用完整性校验

export interface AccumulatedToolCall {
  id: string;
  name: string;
  /** function | custom | tool_search，空串按 function 处理 */
  type?: string;
  arguments: string;
}

export interface ToolValidationError {
  /** 出问题的工具名，用于错误信息 */
  name: string;
  reason: string;
}

export interface ToolBatchValidation {
  /** 只有整批可安全执行时才非空。 */
  executable: AccumulatedToolCall[];
  /** 正常终态下的非法工具调用是上游协议错误。 */
  fatal: ToolValidationError | null;
  /** 截断终态下给用户/模型看的不可执行说明。 */
  incompleteText: string;
}

/**
 * 校验一个累积完成的工具调用是否可以安全交给客户端执行。
 *
 * 为什么必须校验：上游被 `length` / `content_filter` 截断时，arguments 会停在半路。
 * 把残缺 JSON 当成合法工具调用发出去，客户端会真的去执行它 —— 参数缺字段、
 * 或者（更糟）被降级成 `{_raw: "..."}` 这种语义完全不同的调用。
 *
 * 返回 null 表示通过。
 */
export function validateToolCall(t: AccumulatedToolCall): ToolValidationError | null {
  const kind = t.type === "" || t.type === undefined ? "function" : t.type;

  if (t.id === "") {
    return { name: t.name || "(unnamed)", reason: "missing call_id" };
  }
  // tool_search 的 item 形状里本来就没有 name
  if (t.name === "" && kind !== "tool_search") {
    return { name: "(unnamed)", reason: "missing tool name" };
  }

  if (kind === "function" || kind === "tool_search") {
    // 空 arguments 是合法的（无参工具）
    if (t.arguments !== "") {
      try {
        JSON.parse(t.arguments);
      } catch {
        return { name: t.name, reason: "arguments is not valid JSON (upstream likely truncated)" };
      }
    }
    return null;
  }

  if (kind === "custom") {
    // custom 工具的协议语义就是任意自由文本。若内容恰好是 bridge 使用的
    // {input:string} 信封，响应转换层会解开它；否则必须原样交给客户端，不能根据
    // 文本前缀猜测“截断”。传输截断由终态 + validateToolBatch 的整批校验负责。
    return null;
  }

  return { name: t.name, reason: `unsupported tool call type ${JSON.stringify(kind)}` };
}

/** 批量校验，返回第一个错误 */
export function firstToolValidationError(
  tools: AccumulatedToolCall[],
): ToolValidationError | null {
  for (const t of tools) {
    const err = validateToolCall(t);
    if (err) return err;
  }
  return null;
}

/**
 * 对一批并行工具调用做原子校验。
 *
 * 任意一个调用不完整时，整批都不能部分交给客户端执行：否则 A 已执行、B 才报错，
 * 调用方无法回滚。length/content_filter 本身也表示输出不完整，因此这两类终态下
 * 一律只返回不可执行说明。
 */
export function validateToolBatch(
  tools: AccumulatedToolCall[],
  truncated: boolean,
): ToolBatchValidation {
  if (tools.length === 0) return { executable: [], fatal: null, incompleteText: "" };

  const error = firstToolValidationError(tools);
  if (!truncated && error) return { executable: [], fatal: error, incompleteText: "" };
  if (!truncated) return { executable: tools, fatal: null, incompleteText: "" };

  const descriptions = tools.map((tool) => {
    const suffix = tool.arguments === "" ? "" : `: ${tool.arguments.slice(0, 512)}`;
    return `[incomplete tool call ${tool.name || "(unnamed)"}${suffix}]`;
  });
  return { executable: [], fatal: null, incompleteText: descriptions.join("\n") };
}

/**
 * 把工具声明里的 parameters / input_schema 清洗成上游能接受的形状。
 * 缺失时补一个空 object schema（上游要求这个字段存在）。
 */
export function normalizeToolParameters(schema: unknown): unknown {
  if (schema === undefined || schema === null) return { ...EMPTY_TOOL_PARAMETERS };
  if (typeof schema === "string") {
    try {
      return sanitizeJsonSchema(JSON.parse(schema) as unknown);
    } catch {
      // 字符串型 parameters 本身不合标准；不能原样穿透给上游造成二次编码/400。
      return { ...EMPTY_TOOL_PARAMETERS };
    }
  }
  return sanitizeJsonSchema(schema);
}
