import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.native_backend import NativeSDKBackend
from test_native_backend import FakeSdk, MODEL, TOOL


class NativeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.backend = NativeSDKBackend(root / "key", root / "workspace", timeout=3, batch_settle=.005)
        self.backend.workspace.mkdir()
        self.backend.key = "synthetic"
        self.sdk = self.backend.client = FakeSdk()
        async def start_fake():
            if self.backend.client is None:
                self.backend.client = self.sdk
            return self.backend.client
        self.backend._start = start_fake
        self.sdk.scripts = [[("thinking", "synthetic thought"), ("text", "Checking."),
                             ("tools", [("lookup", {"key": "a"})]), ("text", "Done.")]]
        self.service = Service(self.backend, mode="native", ledger_path=root / "results.sqlite3")
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def post(self, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", path, json.dumps(body))
        response = connection.getresponse()
        value = response.read().decode()
        status = response.status
        connection.close()
        return status, json.loads(value)

    def test_responses_pending_survives_busy_drain_and_results_resume_same_run(self):
        body = {"model": MODEL, "tools": [TOOL], "input": [{"role": "user", "content": "lookup"}]}
        status, first = self.post("/v1/responses", body)
        self.assertEqual(status, 200)
        self.assertEqual([item["type"] for item in first["output"]], ["reasoning", "message", "function_call"])
        self.assertEqual(self.post("/admin/drain", {})[0], 409)
        self.assertFalse(self.service.draining)
        call = first["output"][-1]
        second = {**body, "input": body["input"] + first["output"] + [
            {"type": "function_call_output", "call_id": call["call_id"], "output": "synthetic result"}]}
        status, result = self.post("/v1/responses", second)
        self.assertEqual((status, result["output"][0]["content"][0]["text"]), (200, "Done."))
        self.assertEqual(len(self.sdk.sent), 1)
        self.assertEqual(len(self.sdk.callback_results), 1)
        self.assertEqual(self.post("/v1/responses", second)[1], result)
        self.assertEqual(len(self.sdk.sent), 1)

    def test_messages_tool_id_survives_conversion_and_retry(self):
        body = {"model": MODEL, "tools": [{"name": TOOL["name"], "input_schema": TOOL["parameters"]}],
                "messages": [{"role": "user", "content": "lookup"}]}
        status, first = self.post("/v1/messages", body)
        self.assertEqual(status, 200)
        call = first["content"][-1]
        self.assertEqual(call["type"], "tool_use")
        replay = self.post("/v1/messages", body)[1]
        self.assertEqual((replay["id"], replay["content"]), (first["id"], first["content"]))
        second = {**body, "messages": body["messages"] + [{"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "synthetic result"}]}]}
        status, result = self.post("/v1/messages", second)
        self.assertEqual((status, result["content"][0]["text"]), (200, "Done."))
        self.assertEqual(result["usage"]["cache_read_input_tokens"], 80)
        self.assertEqual(len(self.sdk.sent), 1)

    def test_messages_rebuilds_changed_context_without_repeating_tool(self):
        self.sdk.scripts.append([("tools", [("lookup", {"key": "a"})]), ("text", "Recovered.")])
        body = {"model": MODEL, "tools": [{"name": TOOL["name"], "input_schema": TOOL["parameters"]}],
                "messages": [{"role": "user", "content": "original working directory"}]}
        _, first = self.post("/v1/messages", body)
        call = first["content"][-1]
        body["messages"][0]["content"] = "updated working directory"
        body["messages"] += [{"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "executed once"}]}]
        status, result = self.post("/v1/messages", body)
        self.assertEqual(status, 200, result)
        self.assertEqual((status, result["stop_reason"]), (200, "end_turn"))
        self.assertEqual(result["content"], [{"type": "text", "text": "Recovered."}])
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertEqual(self.sdk.callback_results, [{"content": [{"type": "text", "text": "executed once"}]}])
        self.assertEqual(self.post("/v1/messages", body)[1], result)
        self.assertEqual(len(self.sdk.sent), 2)

    def test_messages_accepts_system_context_after_tool_results(self):
        self.sdk.scripts.append([("tools", [("lookup", {"key": "a"})]), ("text", "Recovered.")])
        body = {"model": MODEL, "tools": [{"name": TOOL["name"], "input_schema": TOOL["parameters"]}],
                "messages": [{"role": "user", "content": "continue the task"}]}
        _, first = self.post("/v1/messages", body)
        call = first["content"][-1]
        body["messages"] += [{"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "executed once"}]},
            {"role": "system", "content": "<system-reminder>updated task context</system-reminder>"}]
        status, result = self.post("/v1/messages", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["content"], [{"type": "text", "text": "Recovered."}])
        self.assertIn("updated task context", self.sdk.sent[-1][1])
        self.assertEqual(self.sdk.callback_results, [{"content": [{"type": "text", "text": "executed once"}]}])
