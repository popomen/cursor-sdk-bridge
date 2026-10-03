import { strict as assert } from "node:assert";
import { test } from "node:test";
import { RequestCorrelation } from "../src/request-correlation.js";

const metadata = {
  installation_id: "installation-private",
  session_id: "session-private",
  thread_id: "thread-private",
  turn_id: "turn-private",
  submission_id: "submission-private",
  window_id: "window-private",
  context_window_id: "context-window-private",
  root_turn_id: "root-turn-private",
  parent_turn_id: "parent-turn-private",
  parent_thread_id: "parent-thread-private",
  forked_from_thread_id: "fork-private",
  parent_response_id: "response-private",
  request_kind: "turn",
  turn_trigger: "user",
};

const downstream = {
  model: "m",
  instructions: "private-system-instructions",
  input: [
    {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: "private-user-sentinel" }],
    },
    {
      type: "function_call",
      call_id: "call-private",
      name: "private-tool-name",
      arguments: "{\"private\":true}",
    },
    {
      type: "function_call_output",
      call_id: "call-private",
      output: "private-tool-output",
    },
    { type: "input_image", image_url: "data:image/png;base64,private-image" },
    { type: "reasoning", encrypted_content: "private-reasoning" },
  ],
  client_metadata: {
    thread_id: metadata.thread_id,
    turn_id: metadata.turn_id,
    "x-codex-window-id": metadata.window_id,
    "x-codex-turn-metadata": JSON.stringify(metadata),
  },
};

const upstream = {
  config_name: "cfg",
  model_name: "model",
  session_id: "upstream-session-private",
  conversation_id: "upstream-conversation-private",
  messages: [
    { role: "system", content: [{ type: "text", text: "private-system-instructions" }] },
    { role: "user", content: [{ type: "text", text: "private-user-sentinel" }] },
    {
      role: "assistant",
      content: [{ type: "text", text: " " }],
      tool_calls: [{
        id: "call-private",
        type: "function",
        function_call: { name: "private-tool-name", arguments: "{\"private\":true}" },
      }],
    },
    {
      role: "tool",
      tool_call_id: "call-private",
      content: [{ type: "text", text: "private-tool-output" }],
    },
    {
      role: "user",
      content: [{ type: "image_url", image_url: { url: "data:image/png;base64,private-image" } }],
    },
  ],
  user_input: "",
  tools: [{ type: "function", function: { name: "private-tool-name", parameters: "{}" } }],
  tool_choice: "auto",
  parallel_tool_calls: true,
};

test("request correlation records deterministic HMAC-only context summaries", () => {
  const correlation = new RequestCorrelation("correlation-secret");
  const summary = correlation.summarizeRequest(
    {
      "x-codex-turn-metadata": JSON.stringify(metadata),
      "x-codex-window-id": metadata.window_id,
    },
    downstream,
    upstream,
    "bridge-request-private",
  );

  assert.equal(summary.correlation_version, "hmac-sha256:v1");
  assert.deepEqual(summary.metadata_sources, [
    "body_nested",
    "header",
    "body_flat",
    "compat_header",
  ]);
  assert.equal(summary.metadata_parse_error, false);
  assert.equal(summary.metadata_consistent, true);
  assert.equal(summary.request_kind, "turn");
  assert.equal(summary.turn_trigger, "user");
  assert.equal(summary.input_items, 5);
  assert.deepEqual(summary.input_types, {
    message: 1,
    tool_call: 1,
    tool_output: 1,
    image: 1,
    reasoning: 1,
  });
  assert.equal(summary.messages, 5);
  assert.deepEqual(summary.message_roles, {
    system: 1,
    user: 2,
    assistant: 1,
    tool: 1,
  });
  assert.deepEqual(summary.content_parts, { text: 4, image: 1 });
  assert.equal(summary.tool_calls, 1);
  assert.equal(summary.tools, 1);

  for (const value of [
    summary.bridge_request_hmac,
    summary.input_hmac,
    summary.context_hmac,
    summary.thread_id_hmac,
    summary.turn_id_hmac,
    summary.last_user_hmac,
    summary.last_assistant_hmac,
    summary.last_tool_output_hmac,
    summary.last_tool_call_id_hmac,
  ]) {
    assert.match(value ?? "", /^h1_[A-Za-z0-9_-]{22}$/);
  }

  const serialized = JSON.stringify(summary);
  for (const secret of [
    "correlation-secret",
    "private",
    "thread-private",
    "turn-private",
    "private-user-sentinel",
    "private-tool-output",
    "private-image",
  ]) {
    assert.doesNotMatch(serialized, new RegExp(secret));
  }
});

test("context HMAC is canonical, content-sensitive, and excludes attempt identities", () => {
  const correlation = new RequestCorrelation("same-key");
  const first = correlation.summarizeRequest({}, { input: "one" }, {
    ...upstream,
    messages: [{
      role: "user",
      content: [{ text: "one", type: "text" }],
    }],
  }, "request-one");
  const reordered = correlation.summarizeRequest({}, { input: "one" }, {
    conversation_id: "another-conversation",
    session_id: "another-session",
    parallel_tool_calls: true,
    tool_choice: "auto",
    tools: upstream.tools,
    user_input: "",
    messages: [{
      content: [{ type: "text", text: "one" }],
      role: "user",
    }],
    model_name: "model",
    config_name: "cfg",
  }, "request-two");
  const changed = correlation.summarizeRequest({}, { input: "two" }, {
    messages: [{
      role: "user",
      content: [{ type: "text", text: "two" }],
    }],
  }, "request-three");

  assert.equal(first.context_hmac, reordered.context_hmac);
  assert.notEqual(first.context_hmac, changed.context_hmac);
  assert.notEqual(first.input_hmac, changed.input_hmac);
  assert.notEqual(first.bridge_request_hmac, reordered.bridge_request_hmac);

  const firstAttempt = correlation.summarizeAttempt(first, upstream);
  const secondAttempt = correlation.summarizeAttempt(first, {
    ...upstream,
    session_id: "fresh-session",
    conversation_id: "fresh-conversation",
  });
  assert.equal(firstAttempt.context_hmac, secondAttempt.context_hmac);
  assert.notEqual(firstAttempt.upstream_session_hmac, secondAttempt.upstream_session_hmac);
  assert.notEqual(firstAttempt.upstream_conversation_hmac, secondAttempt.upstream_conversation_hmac);
});

test("metadata projection conflicts and parse failures are explicit without logging values", () => {
  const correlation = new RequestCorrelation("same-key");
  const summary = correlation.summarizeRequest(
    {
      "x-codex-turn-metadata": JSON.stringify({
        thread_id: "header-thread-private",
        turn_id: "shared-turn-private",
        request_kind: "unexpected-private-kind",
        turn_trigger: "unexpected-private-trigger",
      }),
    },
    {
      input: "private-input",
      client_metadata: {
        thread_id: "body-thread-private",
        turn_id: "shared-turn-private",
        "x-codex-turn-metadata": "{malformed-private-json",
      },
    },
    { messages: [{ role: "user", content: [{ type: "text", text: "private-input" }] }] },
    "bridge-private",
  );

  assert.equal(summary.metadata_parse_error, true);
  assert.equal(summary.metadata_consistent, false);
  assert.deepEqual(summary.metadata_mismatch_fields, ["thread_id"]);
  assert.equal(summary.request_kind, "other");
  assert.equal(summary.turn_trigger, "other");
  assert.doesNotMatch(JSON.stringify(summary), /private/);
});

test("queue and provider request identifiers are HMACed under the same request context", () => {
  const correlation = new RequestCorrelation("same-key");
  const request = correlation.summarizeRequest({}, { input: "x" }, {
    messages: [{ role: "user", content: [{ type: "text", text: "x" }] }],
  }, "bridge-id");
  const queue = correlation.summarizeQueue(request, {
    event: "request_wait_in_queue",
    position: 7,
    hasPosition: true,
    queueId: "queue-private",
    requestUuid: "provider-request-private",
    message: "private queue message",
  });

  assert.equal(queue.bridge_request_hmac, request.bridge_request_hmac);
  assert.equal(queue.context_hmac, request.context_hmac);
  assert.match(queue.upstream_queue_hmac ?? "", /^h1_/);
  assert.match(queue.upstream_request_hmac ?? "", /^h1_/);
  assert.doesNotMatch(JSON.stringify(queue), /private/);
});
