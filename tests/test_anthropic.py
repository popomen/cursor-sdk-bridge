import asyncio
import base64
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import cursor_sdk2api
from cursor_sdk_bridge.anthropic_protocol import (OMITTED, complete_message, estimate_tokens, message_shell, prepare_messages,
                                prompt_too_long, stream_events, usage_block)
from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.failures import DeadlineExpired, UpstreamIncomplete
from cursor_sdk_bridge.probe_service import ProbeFailed, probe_messages, quadrant_png
from cursor_sdk_bridge.request_log import REQUEST_STATS, RequestLog
from cursor_sdk_bridge.responses_protocol import MODELS, InvalidRequest, complete_response, request_payload
from cursor_sdk_bridge.sdk_backend import SDKBackend
from test_cursor_sdk2api import StubSDK
from test_sdk_backend import StubClient

MAX = "claude-opus-5-5-max"
BASH = {"name": "Bash", "description": "Run a shell command", "input_schema": {
    "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
    "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}
PNG = base64.b64encode(quadrant_png(["red", "green", "blue", "yellow"])).decode()
SDK_USAGE = {"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 300, "cache_write_tokens": 600}
ANTHROPIC_USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 600,
                   "cache_read_input_tokens": 300}
REPLY = json.dumps({"output": [{"type": "message", "text": "Counting."},
                               {"type": "function_call", "call_id": "call_new", "name": "Bash",
                                "arguments": json.dumps({"command": "ls | wc -l"})}]})


def image(source=None):
    return {"type": "image", "source": source or {"type": "base64", "media_type": "image/png", "data": PNG}}


def claude_code_body(**extra):
    """History shaped like a captured Claude Code 2.1.282 request."""
    return {"model": MAX, "max_tokens": 128000, "stream": True, "thinking": {"type": "adaptive", "display": "omitted"},
            "system": [{"type": "text", "text": "You are Claude Code."},
                       {"type": "text", "text": "Be concise.", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "list files"}]},
                {"role": "system", "content": "mid-conversation reminder", "output_config": {"effort": "medium"}},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "", "signature": "opaque"},
                    {"type": "text", "text": "Listing."},
                    {"type": "tool_use", "id": "toolu_old", "name": "Bash", "input": {"command": "ls"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_old", "content": "a.txt", "is_error": False},
                    {"type": "text", "text": "now count them"}]}],
            "tools": [BASH], "metadata": {"user_id": "synthetic"},
            "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}, **extra}


class UsageSDK(StubSDK):
    async def generate(self, model, prompt, images=()):
        REQUEST_STATS.get()["usage"] = dict(SDK_USAGE)
        return await super().generate(model, prompt)


class TranslationTests(unittest.TestCase):
    def test_claude_code_history_keeps_block_order(self):
        _, history, prompt, images = prepare_messages(claude_code_body())
        self.assertEqual([item.get("type", item.get("role")) for item in history],
                         ["user", "system", "assistant", "function_call", "function_call_output", "user"])
        self.assertEqual(history[3], {"type": "function_call", "call_id": "toolu_old", "name": "Bash",
                                      "arguments": json.dumps({"command": "ls"})})
        self.assertEqual(history[4]["output"], "a.txt")
        payload = request_payload(prompt)
        self.assertEqual(payload["instructions"], "You are Claude Code.\n\nBe concise.")
        self.assertEqual([tool["name"] for tool in payload["tools"]], ["Bash"])
        self.assertEqual((payload["tool_choice"], payload["parallel_tool_calls"]), ("auto", True))
        self.assertTrue(prompt.startswith("You are the model behind a Claude Code Messages adapter."))
        self.assertNotIn("opaque", prompt)
        self.assertEqual(images, [])

    def test_tool_choice_maps_to_responses_rules(self):
        cases = [({"type": "any"}, "required", True), ({"type": "none"}, "none", True),
                 ({"type": "tool", "name": "Bash", "disable_parallel_tool_use": True},
                  {"type": "function", "name": "Bash"}, False)]
        for choice, expected, parallel in cases:
            with self.subTest(choice=choice["type"]):
                payload = request_payload(prepare_messages(claude_code_body(tool_choice=choice))[2])
                self.assertEqual((payload["tool_choice"], payload["parallel_tool_calls"]), (expected, parallel))

    def test_images_from_user_and_tool_result_are_attached_not_inlined(self):
        body = claude_code_body()
        body["messages"][0]["content"].append(image())
        body["messages"][3]["content"][0] = {"type": "tool_result", "tool_use_id": "toolu_old", "is_error": True,
                                             "content": [{"type": "text", "text": "failed"}, image()]}
        _, history, prompt, images = prepare_messages(body)
        self.assertEqual(len(images), 2)
        self.assertNotIn(PNG, prompt)
        self.assertEqual(history[4]["output"][:2], [{"type": "input_text", "text": "[tool_result is_error=true]"},
                                                    {"type": "input_text", "text": "failed"}])

    def test_unsupported_blocks_become_placeholders_and_server_tools_are_dropped(self):
        body = claude_code_body(tools=[BASH, {"type": "web_search_20250305", "name": "web_search"}])
        body["messages"][0]["content"] += [{"type": "document", "source": {"type": "text", "data": "secret"}},
                                           image({"type": "url", "url": "https://example.com/secret.png"})]
        _, history, prompt, images = prepare_messages(body)
        self.assertEqual([part["text"] for part in history[0]["content"][1:]],
                         [OMITTED.format("document"), OMITTED.format("image")])
        self.assertNotIn("secret", prompt)
        self.assertEqual([tool["name"] for tool in request_payload(prompt)["tools"]], ["Bash"])
        self.assertEqual(images, [])

    def test_structural_errors_fail_before_sdk_without_echoing_values(self):
        role, call = claude_code_body(), claude_code_body()
        role["messages"][0]["role"] = "secret-role"
        call["messages"][2]["content"][2]["input"] = "secret-input"
        cases = [(claude_code_body(model="secret-model"), "model"), (role, "messages[0].role"),
                 (call, "messages[2].content[2]")]
        for body, param in cases:
            with self.subTest(param=param):
                with self.assertRaises(InvalidRequest) as caught:
                    prepare_messages(body)
                self.assertEqual(caught.exception.param, param)
                self.assertNotIn("secret", str(caught.exception))


class OutputTests(unittest.TestCase):
    def test_model_output_becomes_text_and_tool_use_with_sdk_usage(self):
        body, history, _, _ = prepare_messages(claude_code_body())
        message = complete_message(message_shell(MAX), complete_response({}, REPLY, body, history), SDK_USAGE)
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["content"][0], {"type": "text", "text": "Counting."})
        call = message["content"][1]
        self.assertEqual((call["type"], call["name"], call["input"]), ("tool_use", "Bash", {"command": "ls | wc -l"}))
        self.assertRegex(call["id"], r"^toolu_[0-9a-f]{24}$")
        self.assertEqual(message["usage"], ANTHROPIC_USAGE)
        text_only = complete_response({}, json.dumps({"output": [{"type": "message", "text": "3"}]}), body, history)
        self.assertEqual(complete_message(message_shell(MAX), text_only)["stop_reason"], "end_turn")
        self.assertEqual(usage_block(None)["input_tokens"], 0)

    def test_stream_events_carry_only_validated_content(self):
        message = {**message_shell(MAX), "stop_reason": "tool_use", "content": [
            {"type": "text", "text": "x" * 600},
            {"type": "tool_use", "id": "toolu_new", "name": "Bash", "input": {"command": "y" * 300}}]}
        events = list(stream_events(message))
        self.assertTrue(all(kind == data["type"] for kind, data in events))
        self.assertEqual([kind for kind, _ in events][-2:], ["message_delta", "message_stop"])
        deltas = {index: "".join(data["delta"].get("text", data["delta"].get("partial_json", ""))
                                 for kind, data in events if kind == "content_block_delta" and data["index"] == index)
                  for index in (0, 1)}
        self.assertEqual(deltas[0], "x" * 600)
        self.assertEqual(json.loads(deltas[1]), {"command": "y" * 300})
        starts = [data["content_block"] for kind, data in events if kind == "content_block_start"]
        self.assertEqual(starts, [{"type": "text", "text": ""},
                                  {"type": "tool_use", "id": "toolu_new", "name": "Bash", "input": {}}])
        self.assertEqual(events[-2][1]["delta"]["stop_reason"], "tool_use")

    def test_token_estimate_and_prompt_too_long_wording(self):
        self.assertEqual(estimate_tokens(2300, ["image"]), 3000 + 1000 + 1600)
        self.assertEqual(prompt_too_long(4600, 2300), "prompt is too long: 5000 tokens > 4000 maximum")


class MessagesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logdir = Path(self.temp.name) / "logs"
        self.sdk = UsageSDK()
        self.service = Service(self.sdk, log=RequestLog(self.logdir))
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        status, content = response.status, response.read().decode()
        connection.close()
        return status, content

    def stream(self, body):
        status, content = self.call("POST", "/v1/messages?beta=true", body)
        self.assertEqual(status, 200)
        events, kind = [], None
        for line in content.splitlines():
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
                self.assertEqual(events[-1]["type"], kind)
        return events

    def log_entries(self, count):
        path = self.logdir / "requests.jsonl"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (path.exists() and len(path.read_text().splitlines()) >= count):
            time.sleep(0.02)
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_stream_text_and_tool_use_with_sdk_usage(self):
        self.sdk.results = [REPLY]
        events = self.stream(claude_code_body())
        self.assertEqual([event["type"] for event in events],
                         ["message_start"] + ["content_block_start", "content_block_delta", "content_block_stop"] * 2
                         + ["message_delta", "message_stop"])
        start = events[0]["message"]
        self.assertEqual((start["role"], start["model"], start["content"]), ("assistant", MAX, []))
        self.assertEqual(events[4]["content_block"]["name"], "Bash")
        self.assertEqual(json.loads(events[5]["delta"]["partial_json"]), {"command": "ls | wc -l"})
        self.assertEqual((events[7]["delta"]["stop_reason"], events[7]["usage"]), ("tool_use", ANTHROPIC_USAGE))
        self.assertEqual([tool["name"] for tool in self.sdk.prompts[0]["tools"]], ["Bash"])
        entry = self.log_entries(1)[-1]
        self.assertEqual((entry["api"], entry["outcome"], entry["usage"]), ("messages", "completed", SDK_USAGE))

    def test_keepalive_pings_while_sdk_runs(self):
        class SlowSDK(UsageSDK):
            async def generate(self, model, prompt, images=()):
                await asyncio.sleep(0.3)
                return await super().generate(model, prompt)
        self.service.backend = SlowSDK()
        self.service.backend.results = [REPLY]
        with patch.object(cursor_sdk2api, "KEEPALIVE_SECONDS", 0.05):
            events = self.stream(claude_code_body())
        kinds = [event["type"] for event in events]
        self.assertGreaterEqual(kinds.count("ping"), 3)
        self.assertEqual(kinds[-1], "message_stop")

    def test_nonstream_returns_one_message(self):
        self.sdk.results = [json.dumps({"output": [{"type": "message", "text": "3"}]})]
        status, content = self.call("POST", "/v1/messages", claude_code_body(stream=False))
        message = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual((message["type"], message["stop_reason"], message["content"]),
                         ("message", "end_turn", [{"type": "text", "text": "3"}]))
        self.assertEqual(message["usage"], ANTHROPIC_USAGE)

    def test_count_tokens_estimates_without_sdk(self):
        body = claude_code_body()
        del body["stream"], body["max_tokens"]
        status, content = self.call("POST", "/v1/messages/count_tokens?beta=true", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"input_tokens": estimate_tokens(len(prepare_messages(body)[2].encode()))})
        self.assertEqual(self.sdk.prompts, [])

    def test_sdk_failures_become_anthropic_errors_without_upstream_text(self):
        self.sdk.results = [DeadlineExpired(), RuntimeError("secret-upstream-body"), UpstreamIncomplete()]
        events = self.stream(claude_code_body())
        self.assertEqual([event["type"] for event in events], ["message_start", "error"])
        self.assertEqual(events[-1]["error"], {"type": "api_error",
                                               "message": "Cursor SDK response failed (deadline_expired)"})
        self.assertNotIn("secret", json.dumps(self.stream(claude_code_body())))
        status, content = self.call("POST", "/v1/messages", claude_code_body(stream=False))
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(content)["error"]["message"], "Cursor SDK response failed (upstream_incomplete)")

    def test_nonstream_failures_tell_claude_code_not_to_retry(self):
        self.sdk.results = [DeadlineExpired()]
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/messages", json.dumps(claude_code_body(stream=False)),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual((response.status, response.getheader("x-should-retry")), (500, "false"))

    def test_prompt_too_long_is_rejected_before_streaming(self):
        self.service.max_prompt_bytes = 100
        status, content = self.call("POST", "/v1/messages", claude_code_body())
        error = json.loads(content)["error"]
        self.assertEqual((status, error["type"]), (400, "invalid_request_error"))
        self.assertTrue(error["message"].startswith("prompt is too long: "), error["message"])
        self.assertEqual(self.sdk.prompts, [])
        self.assertEqual(self.log_entries(1)[-1]["outcome"], "prompt_too_large")

    def test_invalid_requests_use_anthropic_error_shape_without_echo(self):
        status, content = self.call("POST", "/v1/messages", claude_code_body(model="private-model-name"))
        self.assertEqual((status, json.loads(content)["error"]["type"]), (400, "invalid_request_error"))
        self.assertNotIn("private-model-name", content)
        status, content = self.call("POST", "/v1/messages/batches", {})
        self.assertEqual((status, json.loads(content)["error"]["type"]), (404, "not_found_error"))

    def test_models_and_health_serve_both_clients(self):
        listing = json.loads(self.call("GET", "/v1/models?limit=1000")[1])
        self.assertEqual([model["id"] for model in listing["data"]], list(MODELS))
        for model in listing["data"]:
            self.assertEqual((model["type"], model["object"], model["owned_by"]), ("model", "model", "cursor"))
            self.assertTrue(model["display_name"] and model["created_at"])
        self.assertEqual((listing["has_more"], listing["first_id"], listing["last_id"]),
                         (False, next(iter(MODELS)), next(reversed(MODELS))))
        self.assertIn("anthropic_messages", json.loads(self.call("GET", "/health")[1])["capabilities"])


class BackendUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_usage_is_recorded_for_the_request(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "key"
            key.write_text("synthetic-test-key")
            key.chmod(0o600)
            backend = SDKBackend(key, Path(temp) / "workspace", timeout=5)
            backend.key = "synthetic-test-key"
            backend.workspace.mkdir()
            client = backend.client = StubClient()

            async def wait():
                return SimpleNamespace(status="finished", result='{"output":[]}',
                                       model=SimpleNamespace(to_json=lambda: client.options.model),
                                       usage=SimpleNamespace(total_tokens=1950, **SDK_USAGE))
            client.wait = wait
            stats = {}
            REQUEST_STATS.set(stats)
            try:
                self.assertEqual(await backend.generate(MAX, "prompt"), '{"output":[]}')
            finally:
                await backend.close()
        self.assertEqual(stats["usage"], SDK_USAGE)


class EchoSDK(StubSDK):
    """Echoes the probe marker as a tool call, then the tool result as text."""
    suffix = ""

    async def generate(self, model, prompt, images=()):
        payload = request_payload(prompt)
        self.prompts.append(payload)
        results = [item["output"] for item in payload["input"] if item.get("type") == "function_call_output"]
        if results:
            return json.dumps({"output": [{"type": "message", "text": results[-1]}]})
        marker = re.search(r'value "([^"]+)"', payload["input"][0]["content"][0]["text"]).group(1)
        return json.dumps({"output": [{"type": "function_call", "call_id": "call_probe", "name": "fallback_probe_echo",
                                       "arguments": json.dumps({"value": marker + self.suffix})}]})


class MessagesProbeTests(unittest.TestCase):
    def serve(self, sdk):
        service = Service(sdk)
        server = make_server(service, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()
        self.addCleanup(stop)
        return server.server_port

    def test_probe_checks_a_tool_use_and_tool_result_roundtrip(self):
        sdk = EchoSDK()
        self.assertEqual(probe_messages(self.serve(sdk), "high"),
                         {"api": "messages", "effort": "high", "sdk_outputs": 2, "tool_roundtrip": True})
        self.assertEqual([payload["tool_choice"] for payload in sdk.prompts],
                         [{"type": "function", "name": "fallback_probe_echo"}, "none"])

    def test_probe_rejects_wrong_synthetic_arguments(self):
        sdk = EchoSDK()
        sdk.suffix = "-wrong"
        with self.assertRaises(ProbeFailed):
            probe_messages(self.serve(sdk), "high")
        self.assertEqual(len(sdk.prompts), 1)


if __name__ == "__main__":
    unittest.main()
