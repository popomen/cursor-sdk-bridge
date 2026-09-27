import asyncio
import http.client
import json
import threading
import unittest

from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.responses_protocol import MODELS, request_payload

MODEL = "claude-opus-5-5-high"
TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {
    "query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}


class StubSDK:
    def __init__(self):
        self.results, self.prompts = [], []

    async def generate(self, model, prompt):
        self.prompts.append(request_payload(prompt))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        pass


class ResponsesTests(unittest.TestCase):
    def setUp(self):
        self.sdk = StubSDK()
        self.service = Service(self.sdk)
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()

    def post(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/responses", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        status, content = response.status, response.read().decode()
        connection.close()
        if body.get("stream") and status == 200:
            return status, [json.loads(line[6:]) for line in content.splitlines() if line.startswith("data: ")]
        return status, json.loads(content)

    def request(self, **extra):
        return {"model": MODEL, "input": "test", **extra}

    def test_text_sse_full_lifecycle_and_consistent_ids(self):
        self.sdk.results = ['{"output":[{"type":"message","text":"你好\\nworld"}]}']
        status, events = self.post(self.request(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in events], ["response." + suffix for suffix in (
            "created", "in_progress", "output_item.added", "content_part.added", "output_text.delta",
            "output_text.done", "content_part.done", "output_item.done", "completed")])
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
        self.assertEqual(events[0]["response"]["id"], events[-1]["response"]["id"])
        self.assertEqual(events[2]["item"]["content"], [])
        self.assertEqual(events[4]["delta"], "你好\nworld")
        self.assertEqual(events[4]["item_id"], events[-2]["item"]["id"])

    def test_function_roundtrip_previous_response(self):
        self.sdk.results = [json.dumps({"output": [{"type": "function_call", "call_id": "lookup_1",
            "name": "lookup", "arguments": '{"query":"quoted \\\" value\\nline"}'}]}),
            '{"output":[{"type":"message","text":"Found result"}]}']
        _, events = self.post(self.request(stream=True, tools=[TOOL]))
        self.assertEqual([e["type"] for e in events], ["response." + suffix for suffix in (
            "created", "in_progress", "output_item.added", "function_call_arguments.delta",
            "function_call_arguments.done", "output_item.done", "completed")])
        response = events[-1]["response"]
        status, result = self.post(self.request(tools=[TOOL], previous_response_id=response["id"], input=[
            {"type": "function_call_output", "call_id": "lookup_1", "output": "RESULT"}]))
        self.assertEqual(status, 200)
        self.assertEqual(result["output"][0]["content"][0]["text"], "Found result")
        history = self.sdk.prompts[1]["input"]
        self.assertEqual(history[-2]["call_id"], history[-1]["call_id"])
        self.assertEqual(history[-1]["output"], "RESULT")

    def test_function_roundtrip_full_history(self):
        self.sdk.results = ['{"output":[{"type":"message","text":"done"}]}']
        history = [{"role": "user", "content": "lookup"}, {"type": "function_call", "call_id": "c1",
            "name": "lookup", "arguments": '{"query":"hello"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "found"}]
        status, _ = self.post(self.request(input=history, tools=[TOOL]))
        self.assertEqual(status, 200)
        self.assertEqual(self.sdk.prompts[0]["input"], history)

    def test_namespace_routing_and_structured_output_roundtrip(self):
        tools = [{"type": "namespace", "name": namespace, "description": "synthetic",
                  "tools": [TOOL]} for namespace in ("alpha", "beta")]
        self.sdk.results = [json.dumps({"output": [{"type": "function_call", "call_id": "n1",
            "name": "beta.lookup", "arguments": '{"query":"hello"}'}]}),
            '{"output":[{"type":"message","text":"done"}]}']
        _, events = self.post(self.request(stream=True, tools=tools,
            tool_choice={"type": "function", "namespace": "beta", "name": "lookup"}))
        response = events[-1]["response"]
        call = response["output"][0]
        self.assertEqual((call["namespace"], call["name"]), ("beta", "lookup"))
        self.assertEqual(events[2]["item"]["namespace"], "beta")
        self.assertEqual([t["name"] for t in self.sdk.prompts[0]["tools"]], ["alpha.lookup", "beta.lookup"])
        status, result = self.post(self.request(tools=tools, previous_response_id=response["id"], input=[
            {"type": "function_call_output", "call_id": "n1", "output": [{"type": "input_text", "text": "found"}]}]))
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.sdk.prompts[1]["input"][-2]["name"], "beta.lookup")
        self.assertEqual(self.sdk.prompts[1]["input"][-1]["call_id"], "n1")

    def test_safe_error_identifies_unsupported_nested_tool_without_echoing_values(self):
        status, result = self.post(self.request(tools=[{"type": "namespace", "name": "private-name",
            "tools": [{"type": "custom", "name": "private-tool", "description": "secret-value"}]}]))
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["param"], "tools[0].tools[0].type")
        self.assertEqual(result["error"]["code"], "unsupported_request")
        for private in ("private-name", "private-tool", "secret-value"):
            self.assertNotIn(private, json.dumps(result))

    def test_namespace_cannot_route_to_another_tool_with_same_unqualified_name(self):
        self.sdk.results = [json.dumps({"output": [{"type": "function_call", "call_id": "c",
            "name": "lookup", "arguments": '{"query":"x"}'}]})]
        status, _ = self.post(self.request(tools=[{"type": "namespace", "name": "alpha", "tools": [TOOL]}]))
        self.assertEqual(status, 502)

    def test_malformed_unknown_schema_and_duplicate_outputs_fail_without_partial_calls(self):
        cases = ['```json\n{}\n```', '{"output":[],"output":[]}',
            json.dumps({"output": [{"type": "function_call", "call_id": "c", "name": "unknown", "arguments": "{}"}]}),
            json.dumps({"output": [{"type": "function_call", "call_id": "c", "name": "lookup", "arguments": '{"query":2}'}]})]
        for text in cases:
            with self.subTest(text=text):
                self.sdk.results = [text]
                _, events = self.post(self.request(stream=True, tools=[TOOL]))
                self.assertEqual([e["type"] for e in events], ["response.created", "response.in_progress", "response.failed"])

    def test_sdk_error_redacted_and_no_completed(self):
        self.sdk.results = [RuntimeError("secret-api-key-in-upstream-error")]
        _, events = self.post(self.request(stream=True))
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("secret-api-key", json.dumps(events))

    def test_sdk_timeout_emits_failed(self):
        self.sdk.results = [asyncio.TimeoutError()]
        _, events = self.post(self.request(stream=True))
        self.assertEqual(events[-1]["type"], "response.failed")

    def test_nonstream_timeout_returns_502(self):
        self.sdk.results = [asyncio.TimeoutError()]
        status, result = self.post(self.request())
        self.assertEqual(status, 502)
        self.assertEqual(result["error"]["code"], "deadline_expired")

    def test_tool_choice_and_parallel_calls_enforced(self):
        calls = [{"type": "function_call", "call_id": f"c{i}", "name": "lookup", "arguments": '{"query":"q"}'} for i in range(2)]
        for extra in ({"tool_choice": "none"}, {"parallel_tool_calls": False}):
            self.sdk.results = [json.dumps({"output": calls})]
            status, _ = self.post(self.request(tools=[TOOL], **extra))
            self.assertEqual(status, 502)

    def test_store_false_and_unknown_previous_rejected(self):
        self.sdk.results = ['{"output":[{"type":"message","text":"done"}]}']
        _, response = self.post(self.request(store=False))
        status, _ = self.post(self.request(previous_response_id=response["id"]))
        self.assertEqual(status, 400)
        self.assertEqual(len(self.sdk.prompts), 1)

    def test_unsupported_inputs_rejected_before_sdk(self):
        for extra in ({"tools": [{"type": "custom", "name": "apply_patch"}]},
                      {"reasoning": {"effort": "ultra"}}, {"model": "gpt-6-astra"},
                      {"input": [{"type": "function_call_output", "call_id": "orphan", "output": "x"}]},
                      {"input": [{"role": "user", "content": [{"type": "input_audio", "audio_url": "x"}]}]}):
            with self.subTest(extra=extra):
                status, _ = self.post(self.request(**extra))
                self.assertEqual(status, 400)
        self.assertFalse(self.sdk.prompts)

    def test_models_advertise_all_variants(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        self.assertEqual([m["id"] for m in json.load(response)["data"]],
                         list(MODELS))
        connection.close()


class ShutdownTests(unittest.TestCase):
    def test_close_cancels_and_awaits_inflight_requests(self):
        started, cleaned = threading.Event(), threading.Event()
        class WaitingSDK(StubSDK):
            async def generate(self, model, prompt):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cleaned.set()
        service = Service(WaitingSDK())
        body = {"model": MODEL, "input": "wait"}
        shell, history, prompt, _ = service.prepare(body)
        future = service.submit(shell, body, history, prompt)
        self.assertTrue(started.wait(2))
        service.close()
        self.assertTrue(future.done())
        self.assertTrue(cleaned.is_set())
        with self.assertRaises(RuntimeError):
            service.submit(shell, body, history, prompt)


if __name__ == "__main__":
    unittest.main()
