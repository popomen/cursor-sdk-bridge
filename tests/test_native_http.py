import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import cursor_sdk2api
from cursor_sdk_bridge.anthropic_protocol import estimate_tokens, prepare_messages
from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.native_backend import NativeSDKBackend
from cursor_sdk_bridge.request_log import RequestLog
from cursor_sdk_bridge.sdk_backend import Slots
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

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            time.sleep(0.01)

    def stream(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/responses", json.dumps({**body, "stream": True}))
        return connection, connection.getresponse()

    def start_blocking_first(self):
        gate = asyncio.Event()
        self.backend.max_concurrency, self.backend.slots = 1, Slots(1)
        self.sdk.scripts = [[("wait", gate), ("text", "first")], [("text", "second")]]
        results = []
        thread = threading.Thread(target=lambda: results.append(
            self.post("/v1/responses", {"model": MODEL, "input": "first"})))
        thread.start()
        self.wait_for(lambda: len(self.backend.progress()["runs"]) == 1)
        return gate, thread, results

    def abandon_queued(self, body):
        connection, response = self.stream(body)
        self.wait_for(lambda: self.backend.progress()["queued"] == 1)
        response.close()
        connection.close()

    def test_queued_request_nobody_awaits_is_cancelled_after_the_grace(self):
        logs = Path(self.temp.name) / "logs"
        self.service.log = RequestLog(logs)
        with patch.object(cursor_sdk2api, "KEEPALIVE_SECONDS", 0.05), patch.object(cursor_sdk2api, "ORPHAN_GRACE", 0.1):
            gate, thread, results = self.start_blocking_first()
            self.abandon_queued({"model": MODEL, "input": "second"})
            self.wait_for(lambda: len(self.service.jobs) == 1 and self.backend.progress()["queued"] == 0)
        self.service.loop.call_soon_threadsafe(gate.set)
        thread.join(5)
        self.assertEqual(results[0][1]["output"][0]["content"][0]["text"], "first")
        self.wait_for(lambda: self.service.lifecycle()["unfinished"] == 0)
        self.assertEqual(len(self.sdk.sent), 1)
        entries = [json.loads(line) for line in (logs / "requests.jsonl").read_text().splitlines()]
        self.assertIn(("inference", "orphan_cancelled"), [(entry["event"], entry["outcome"]) for entry in entries])

    def test_retry_within_the_grace_joins_the_queued_job(self):
        body = {"model": MODEL, "input": "second"}
        with patch.object(cursor_sdk2api, "KEEPALIVE_SECONDS", 0.05), patch.object(cursor_sdk2api, "ORPHAN_GRACE", 0.3):
            gate, thread, _ = self.start_blocking_first()
            self.abandon_queued(body)
            connection, response = self.stream(body)
            time.sleep(0.6)
            self.assertEqual((self.backend.progress()["queued"], len(self.service.jobs)), (1, 2))
            self.service.loop.call_soon_threadsafe(gate.set)
            content = response.read().decode()
            connection.close()
        thread.join(5)
        events = [json.loads(line[6:]) for line in content.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], "second")
        self.assertEqual(len(self.sdk.sent), 2)

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

    def test_force_drain_closes_admission_despite_pending_work(self):
        body = {"model": MODEL, "tools": [TOOL], "input": [{"role": "user", "content": "lookup"}]}
        self.assertEqual(self.post("/v1/responses", body)[0], 200)
        status, state = self.post("/admin/drain?force=1", {})
        self.assertEqual((status, state["draining"], state["unfinished"]), (200, True, 1))
        self.assertTrue(self.service.draining)

    def test_oversized_tool_continuation_frees_its_pending_run(self):
        body = {"model": MODEL, "tools": [TOOL], "input": [{"role": "user", "content": "lookup"}]}
        status, first = self.post("/v1/responses", body)
        self.assertEqual((status, self.backend.pending_count()), (200, 1))
        call = first["output"][-1]
        second = {**body, "input": body["input"] + first["output"] + [
            {"type": "function_call_output", "call_id": call["call_id"], "output": "synthetic result"}]}
        self.service.context_prompt_bytes = {"1m": 1}
        status, result = self.post("/v1/responses", second)
        self.assertEqual((status, result["error"]["code"]), (400, "context_length_exceeded"))
        for _ in range(200):
            if self.backend.pending_count() == 0:
                break
            time.sleep(0.01)
        self.assertEqual(self.backend.pending_count(), 0)
        self.assertTrue(self.sdk.runs[0].cancelled)

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
        self.assertEqual(len(self.sdk.sent), 1)
        # The fake run finishes with 100 SDK input tokens summed over its model calls; each response
        # instead reports its own request size, and the tool-use response is not zero.
        for message, request in ((first, body), (result, second)):
            prompt_bytes = len(prepare_messages(request)[2].encode())
            self.assertEqual(message["usage"]["input_tokens"], estimate_tokens(prompt_bytes))
            self.assertEqual(message["usage"]["cache_read_input_tokens"], 0)
            self.assertGreater(message["usage"]["output_tokens"], 0)

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

    def test_messages_text_after_tool_results_reaches_the_paused_run(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]), ("text", "Continued.")]]
        body = {"model": MODEL, "tools": [{"name": TOOL["name"], "input_schema": TOOL["parameters"]}],
                "messages": [{"role": "user", "content": "continue the task"}]}
        _, first = self.post("/v1/messages", body)
        call = first["content"][-1]
        reminder = "<system-reminder>updated task context</system-reminder>"
        body["messages"] += [{"role": "assistant", "content": first["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": "executed once"},
            {"type": "text", "text": reminder}]}]
        status, result = self.post("/v1/messages", body)
        self.assertEqual((status, result["content"]), (200, [{"type": "text", "text": "Continued."}]))
        self.assertEqual(len(self.sdk.sent), 1)
        [delivered] = self.sdk.callback_results
        texts = [part["text"] for part in delivered["content"]]
        self.assertEqual((texts[0], texts[2:]), ("executed once", [reminder]))
        self.assertRegex(texts[1], r"^\[client messages [0-9a-f]{12}\]$")
        self.assertIn(texts[1], self.sdk.sent[0][1])

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
