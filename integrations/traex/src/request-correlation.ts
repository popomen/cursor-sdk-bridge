/**
 * Request correlation without retaining prompts or stable client identifiers.
 *
 * Every value derived from content or identity is HMACed with a process-local
 * key. The summaries are safe to emit at info level and remain comparable for
 * the lifetime of a deployment.
 */

import { createHmac, randomBytes, type Hmac } from "node:crypto";
import type { IncomingHttpHeaders } from "node:http";
import type { QueueEvent } from "./types/unified.js";

const FINGERPRINT_VERSION = "hmac-sha256:v1";
const MAX_METADATA_BYTES = 32 * 1024;

const IDENTITY_KEYS = [
  "installation_id",
  "session_id",
  "thread_id",
  "turn_id",
  "submission_id",
  "window_id",
  "context_window_id",
  "root_turn_id",
  "parent_turn_id",
  "parent_thread_id",
  "forked_from_thread_id",
  "parent_response_id",
] as const;

const IDENTITY_ALIASES: Readonly<Record<string, readonly string[]>> = {
  installation_id: ["installation_id", "x-codex-installation-id"],
  session_id: ["session_id"],
  thread_id: ["thread_id"],
  turn_id: ["turn_id"],
  submission_id: ["submission_id"],
  window_id: ["window_id", "x-codex-window-id"],
  context_window_id: ["context_window_id"],
  root_turn_id: ["root_turn_id"],
  parent_turn_id: ["parent_turn_id"],
  parent_thread_id: ["parent_thread_id", "x-codex-parent-thread-id"],
  forked_from_thread_id: ["forked_from_thread_id"],
  parent_response_id: ["parent_response_id"],
};

type IdentityKey = typeof IDENTITY_KEYS[number];
type Headers = IncomingHttpHeaders | Record<string, string | string[] | undefined>;
type PlainObject = Record<string, unknown>;

interface MetadataSource {
  name: "header" | "body_nested" | "body_flat" | "compat_header";
  value: PlainObject;
}

export interface RequestCorrelationSummary {
  correlation_version: typeof FINGERPRINT_VERSION;
  bridge_request_hmac: string;
  input_hmac?: string;
  context_hmac: string;
  metadata_sources: string[];
  metadata_parse_error: boolean;
  metadata_consistent: boolean;
  metadata_mismatch_fields?: string[];
  request_kind?: "turn" | "prewarm" | "compaction" | "memory" | "other";
  turn_trigger?: string;
  installation_id_hmac?: string;
  session_id_hmac?: string;
  thread_id_hmac?: string;
  turn_id_hmac?: string;
  submission_id_hmac?: string;
  window_id_hmac?: string;
  context_window_id_hmac?: string;
  root_turn_id_hmac?: string;
  parent_turn_id_hmac?: string;
  parent_thread_id_hmac?: string;
  forked_from_thread_id_hmac?: string;
  parent_response_id_hmac?: string;
  input_items: number;
  input_types: Record<string, number>;
  messages: number;
  message_roles: Record<string, number>;
  content_parts: Record<string, number>;
  tool_calls: number;
  tools: number;
  last_user_hmac?: string;
  last_assistant_hmac?: string;
  last_tool_output_hmac?: string;
  last_tool_call_id_hmac?: string;
}

export interface UpstreamAttemptSummary {
  bridge_request_hmac: string;
  context_hmac: string;
  upstream_session_hmac?: string;
  upstream_conversation_hmac?: string;
}

export interface UpstreamQueueSummary {
  bridge_request_hmac: string;
  context_hmac: string;
  upstream_queue_hmac?: string;
  upstream_request_hmac?: string;
}

function isPlainObject(value: unknown): value is PlainObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

function headerValue(headers: Headers, name: string): string | undefined {
  const direct = headers[name];
  const value = direct ?? Object.entries(headers)
    .find(([key]) => key.toLowerCase() === name)?.[1];
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value[0];
  return undefined;
}

function parseMetadata(value: unknown): { value?: PlainObject; error: boolean } {
  if (isPlainObject(value)) return { value, error: false };
  if (typeof value !== "string" || value === "") return { error: false };
  if (Buffer.byteLength(value) > MAX_METADATA_BYTES) return { error: true };
  try {
    const parsed = JSON.parse(value) as unknown;
    return isPlainObject(parsed) ? { value: parsed, error: !isPlainObject(parsed) } : { error: true };
  } catch {
    return { error: true };
  }
}

function identityValue(source: PlainObject, key: IdentityKey): string | undefined {
  for (const alias of IDENTITY_ALIASES[key] ?? [key]) {
    const value = stringValue(source[alias]);
    if (value !== undefined) return value;
  }
  if (key === "submission_id" && isPlainObject(source["submission"])) {
    return stringValue(source["submission"]["id"]);
  }
  return undefined;
}

function canonicalUpdate(hash: Hmac, value: unknown, seen: WeakSet<object>, depth = 0): void {
  if (depth > 100) {
    hash.update("depth-limit;");
    return;
  }
  if (value === null) {
    hash.update("null;");
    return;
  }
  switch (typeof value) {
    case "string":
      hash.update(`string:${Buffer.byteLength(value)}:`);
      hash.update(value);
      hash.update(";");
      return;
    case "number":
      hash.update(`number:${Number.isFinite(value) ? value : "null"};`);
      return;
    case "boolean":
      hash.update(value ? "boolean:true;" : "boolean:false;");
      return;
    case "undefined":
      hash.update("undefined;");
      return;
    case "bigint":
      hash.update(`bigint:${value.toString()};`);
      return;
    case "object":
      break;
    default:
      hash.update(`${typeof value};`);
      return;
  }

  if (seen.has(value)) {
    hash.update("circular;");
    return;
  }
  seen.add(value);
  if (Array.isArray(value)) {
    hash.update(`array:${value.length}:[`);
    for (const item of value) canonicalUpdate(hash, item, seen, depth + 1);
    hash.update("];");
  } else {
    const object = value as PlainObject;
    const keys = Object.keys(object).filter((key) => object[key] !== undefined).sort();
    hash.update(`object:${keys.length}:{`);
    for (const key of keys) {
      canonicalUpdate(hash, key, seen, depth + 1);
      canonicalUpdate(hash, object[key], seen, depth + 1);
    }
    hash.update("};");
  }
  seen.delete(value);
}

function increment(target: Record<string, number>, key: string): void {
  target[key] = (target[key] ?? 0) + 1;
}

function inputType(value: unknown): string {
  if (!isPlainObject(value)) return typeof value === "string" ? "text" : "other";
  const type = stringValue(value["type"]) ?? "message";
  if (type === "message" || type === "agent_message") return type;
  if (type === "function_call" || type === "custom_tool_call" || type === "tool_search_call") {
    return "tool_call";
  }
  if (type === "function_call_output" || type === "custom_tool_call_output" ||
      type === "tool_search_output") {
    return "tool_output";
  }
  if (type === "reasoning") return "reasoning";
  if (type === "item_reference") return "item_reference";
  if (type.includes("image")) return "image";
  if (type.includes("audio")) return "audio";
  if (type.includes("file")) return "file";
  if (type === "additional_tools") return "additional_tools";
  return "other";
}

function roleType(value: unknown): string {
  switch (value) {
    case "system":
    case "user":
    case "assistant":
    case "tool":
      return value;
    default:
      return "other";
  }
}

function partType(value: unknown): string {
  if (!isPlainObject(value)) return "other";
  switch (value["type"]) {
    case "text":
      return "text";
    case "image":
    case "image_url":
      return "image";
    case "audio":
    case "audio_url":
    case "input_audio":
      return "audio";
    default:
      return "other";
  }
}

function requestKind(value: unknown): RequestCorrelationSummary["request_kind"] {
  if (value === undefined) return undefined;
  if (value === "turn" || value === "prewarm" || value === "compaction" || value === "memory") {
    return value;
  }
  return "other";
}

const KNOWN_TURN_TRIGGERS = new Set([
  "user",
  "goal",
  "queue",
  "retry",
  "realtime",
  "memory_consolidation",
  "guardian_review",
  "composer",
  "automation_cron_scheduled",
]);

function turnTrigger(value: unknown): string | undefined {
  if (typeof value !== "string" || value === "") return undefined;
  return KNOWN_TURN_TRIGGERS.has(value) ? value : "other";
}

export class RequestCorrelation {
  private readonly key: Buffer;

  constructor(secret = "") {
    const material = secret === "" ? randomBytes(32) : Buffer.from(secret, "utf8");
    this.key = createHmac("sha256", material)
      .update("traex-bridge/request-correlation/key/v1")
      .digest();
  }

  private fingerprint(domain: string, value: unknown): string {
    const hash = createHmac("sha256", this.key);
    hash.update(domain);
    hash.update("\0");
    canonicalUpdate(hash, value, new WeakSet());
    return `h1_${hash.digest("base64url").slice(0, 22)}`;
  }

  summarizeRequest(
    headers: Headers,
    downstreamBody: PlainObject,
    upstreamBody: PlainObject,
    bridgeRequestId: string,
  ): RequestCorrelationSummary {
    const metadataHeader = headerValue(headers, "x-codex-turn-metadata");
    const parsedHeader = parseMetadata(metadataHeader);
    const clientMetadata = isPlainObject(downstreamBody["client_metadata"])
      ? downstreamBody["client_metadata"] as PlainObject
      : undefined;
    const parsedNested = parseMetadata(clientMetadata?.["x-codex-turn-metadata"]);
    const sources: MetadataSource[] = [];
    if (parsedNested.value) sources.push({ name: "body_nested", value: parsedNested.value });
    if (parsedHeader.value) sources.push({ name: "header", value: parsedHeader.value });
    if (clientMetadata) sources.push({ name: "body_flat", value: clientMetadata });

    const compat: PlainObject = {};
    const windowId = headerValue(headers, "x-codex-window-id");
    const installationId = headerValue(headers, "x-codex-installation-id");
    const parentThreadId = headerValue(headers, "x-codex-parent-thread-id");
    const submissionId = headerValue(headers, "x-codex-submission-id");
    if (windowId) compat["window_id"] = windowId;
    if (installationId) compat["installation_id"] = installationId;
    if (parentThreadId) compat["parent_thread_id"] = parentThreadId;
    if (submissionId) compat["submission_id"] = submissionId;
    if (Object.keys(compat).length > 0) {
      sources.push({ name: "compat_header", value: compat });
    }

    const identities = {} as Partial<Record<IdentityKey, string>>;
    const mismatches: string[] = [];
    for (const key of IDENTITY_KEYS) {
      const values = sources
        .map((source) => identityValue(source.value, key))
        .filter((value): value is string => value !== undefined);
      if (values.length > 0) identities[key] = values[0];
      if (new Set(values).size > 1) mismatches.push(key);
    }

    const input = downstreamBody["input"];
    const inputValues = Array.isArray(input) ? input : input === undefined ? [] : [input];
    const inputTypes: Record<string, number> = {};
    for (const item of inputValues) increment(inputTypes, inputType(item));

    const messages = Array.isArray(upstreamBody["messages"])
      ? upstreamBody["messages"] as unknown[]
      : [];
    const messageRoles: Record<string, number> = {};
    const contentParts: Record<string, number> = {};
    let toolCalls = 0;
    let lastUser: unknown;
    let lastAssistant: unknown;
    let lastToolOutput: unknown;
    let lastToolCallId: string | undefined;
    for (const message of messages) {
      if (!isPlainObject(message)) {
        increment(messageRoles, "other");
        continue;
      }
      const role = roleType(message["role"]);
      increment(messageRoles, role);
      if (role === "user") lastUser = message["content"];
      if (role === "assistant") lastAssistant = message["content"];
      if (role === "tool") {
        lastToolOutput = message["content"];
        lastToolCallId = stringValue(message["tool_call_id"]);
      }
      const calls = message["tool_calls"];
      if (Array.isArray(calls)) toolCalls += calls.length;
      const content = Array.isArray(message["content"]) ? message["content"] as unknown[] : [message["content"]];
      for (const part of content) increment(contentParts, partType(part));
    }

    const context = {
      messages: upstreamBody["messages"],
      user_input: upstreamBody["user_input"],
      tools: upstreamBody["tools"],
      tool_choice: upstreamBody["tool_choice"],
      parallel_tool_calls: upstreamBody["parallel_tool_calls"],
    };
    const chosenMetadata = sources[0]?.value;
    const summary: RequestCorrelationSummary = {
      correlation_version: FINGERPRINT_VERSION,
      bridge_request_hmac: this.fingerprint("bridge-request-id", bridgeRequestId),
      ...(input === undefined ? {} : { input_hmac: this.fingerprint("downstream-input", input) }),
      context_hmac: this.fingerprint("upstream-context", context),
      metadata_sources: sources.map((source) => source.name),
      metadata_parse_error: parsedHeader.error || parsedNested.error,
      metadata_consistent: mismatches.length === 0,
      ...(mismatches.length > 0 ? { metadata_mismatch_fields: mismatches } : {}),
      ...(chosenMetadata ? { request_kind: requestKind(chosenMetadata["request_kind"]) } : {}),
      ...(chosenMetadata ? { turn_trigger: turnTrigger(chosenMetadata["turn_trigger"]) } : {}),
      input_items: inputValues.length,
      input_types: inputTypes,
      messages: messages.length,
      message_roles: messageRoles,
      content_parts: contentParts,
      tool_calls: toolCalls,
      tools: Array.isArray(upstreamBody["tools"]) ? upstreamBody["tools"].length : 0,
      ...(lastUser === undefined ? {} : { last_user_hmac: this.fingerprint("message-content", lastUser) }),
      ...(lastAssistant === undefined
        ? {}
        : { last_assistant_hmac: this.fingerprint("message-content", lastAssistant) }),
      ...(lastToolOutput === undefined
        ? {}
        : { last_tool_output_hmac: this.fingerprint("message-content", lastToolOutput) }),
      ...(lastToolCallId === undefined
        ? {}
        : { last_tool_call_id_hmac: this.fingerprint("identifier", lastToolCallId) }),
    };
    for (const key of IDENTITY_KEYS) {
      const value = identities[key];
      if (value !== undefined) {
        summary[`${key}_hmac` as `${IdentityKey}_hmac`] = this.fingerprint("identifier", value);
      }
    }
    return summary;
  }

  summarizeAttempt(
    request: Pick<RequestCorrelationSummary, "bridge_request_hmac" | "context_hmac">,
    upstreamBody: PlainObject,
  ): UpstreamAttemptSummary {
    const sessionId = stringValue(upstreamBody["session_id"]);
    const conversationId = stringValue(upstreamBody["conversation_id"]);
    return {
      bridge_request_hmac: request.bridge_request_hmac,
      context_hmac: request.context_hmac,
      ...(sessionId ? { upstream_session_hmac: this.fingerprint("identifier", sessionId) } : {}),
      ...(conversationId
        ? { upstream_conversation_hmac: this.fingerprint("identifier", conversationId) }
        : {}),
    };
  }

  summarizeQueue(
    request: Pick<RequestCorrelationSummary, "bridge_request_hmac" | "context_hmac">,
    queue: QueueEvent,
  ): UpstreamQueueSummary {
    return {
      bridge_request_hmac: request.bridge_request_hmac,
      context_hmac: request.context_hmac,
      ...(queue.queueId
        ? { upstream_queue_hmac: this.fingerprint("identifier", queue.queueId) }
        : {}),
      ...(queue.requestUuid
        ? { upstream_request_hmac: this.fingerprint("identifier", queue.requestUuid) }
        : {}),
    };
  }
}
