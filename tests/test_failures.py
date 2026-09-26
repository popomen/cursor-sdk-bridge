import asyncio
import concurrent.futures
import http.client
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import cursor_sdk2api
from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.failures import (DeadlineExpired, InvalidRequest, IsolationFailed, KeyInvalid, ModelMismatch, QueueTimeout, UpstreamIncomplete,
                      error_code, failure_label)
from cursor_sdk_bridge.request_log import REQUEST_STATS, RequestLog
from cursor_sdk_bridge.responses_protocol import FORMAT_REMINDER, prepare_request, request_payload
from cursor_sdk_bridge.sdk_backend import SDKBackend
from test_cursor_sdk2api import MODEL, StubSDK
from test_sdk_backend import StubClient

OK = '{"output":[{"type":"message","text":"ok"}]}'


class SlowSDK(StubSDK):
    def __init__(self, seconds, result=OK):
        super().__init__()
        self.seconds, self.result = seconds, result

    async def generate(self, model, prompt):
        await asyncio.sleep(self.seconds)
        return self.result


class InnerDeadlineSDK(StubSDK):
    async def generate(self, model, prompt):
        return await asyncio.wait_for(asyncio.sleep(5), 0.05)


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logdir = Path(self.temp.name) / "logs"
        self.sdk = StubSDK()
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

    def raw_post(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/responses", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        status, content = response.status, response.read().decode()
        connection.close()
        return status, content

    def post(self, body):
        status, content = self.raw_post(body)
        if body.get("stream") and status == 200:
            return status, [json.loads(line[6:]) for line in content.splitlines() if line.startswith("data: ")]
        return status, json.loads(content)

    def failed(self, extra):
        _, events = self.post({"model": MODEL, "input": "test", "stream": True, **extra})
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("response.completed", [e["type"] for e in events])
        return events, events[-1]["response"]["error"]

    def log_entries(self, count):
        path = self.logdir / "requests.jsonl"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (path.exists() and len(path.read_text().splitlines()) >= count):
            time.sleep(0.02)
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_labels_distinguish_failures_without_echoing_upstream_text(self):
        cases = [(DeadlineExpired(), "deadline_expired"), (UpstreamIncomplete(), "upstream_incomplete"),
                 (ModelMismatch(), "model_mismatch"), (IsolationFailed(), "isolation_failed"),
                 (KeyInvalid(), "key_invalid"), (RuntimeError("secret-upstream-body"), "upstream_error:RuntimeError"),
                 (InvalidRequest("secret-request-body"), "invalid_request"),
                 ("secret-model-text is not JSON", "invalid_model_output")]
        for result, label in cases:
            with self.subTest(label=label):
                self.sdk.results = [result]
                events, error = self.failed({})
                self.assertEqual(error["code"], label)
                self.assertEqual(error["message"], "Cursor SDK response failed (" + label + ")")
                self.assertNotIn("secret", json.dumps(events))

    def test_nonstream_failure_label_in_502(self):
        self.sdk.results = [UpstreamIncomplete()]
        status, result = self.post({"model": MODEL, "input": "test"})
        self.assertEqual((status, result["error"]["code"]), (502, "upstream_incomplete"))

    def test_backend_request_rejection_is_400_without_private_details(self):
        self.sdk.results = [InvalidRequest("private request detail")]
        status, result = self.post({"model": MODEL, "input": "test"})
        self.assertEqual((status, result["error"]["code"]), (400, "invalid_request"))
        self.assertNotIn("private", json.dumps(result))

    def test_inner_sdk_timeout_is_deadline_not_runtime_error(self):
        # Python 3.11 aliases all three; the old handler turned an inner deadline into RuntimeError.
        self.assertIs(concurrent.futures.TimeoutError, TimeoutError)
        self.assertIs(asyncio.TimeoutError, TimeoutError)
        self.service.backend = InnerDeadlineSDK()
        _, error = self.failed({})
        self.assertEqual(error["code"], "deadline_expired")

    def test_service_deadline_is_request_timeout(self):
        self.service.backend, self.service.timeout = SlowSDK(5), 0.2
        _, error = self.failed({})
        self.assertEqual(error["code"], "request_timeout")

    def test_oversized_prompt_is_context_length_exceeded_before_sdk(self):
        self.service.max_prompt_bytes = 100
        _, error = self.failed({})
        self.assertEqual(error["code"], "context_length_exceeded")
        status, result = self.post({"model": MODEL, "input": "test"})
        self.assertEqual((status, result["error"]["code"]), (400, "context_length_exceeded"))
        self.assertFalse(self.sdk.prompts)

    def test_keepalive_uses_data_events_not_comments(self):
        self.service.backend = SlowSDK(0.35)
        with patch.object(cursor_sdk2api, "KEEPALIVE_SECONDS", 0.1):
            status, content = self.raw_post({"model": MODEL, "input": "test", "stream": True})
        self.assertEqual(status, 200)
        self.assertFalse([line for line in content.splitlines() if line.startswith(":")])
        events = [json.loads(line[6:]) for line in content.splitlines() if line.startswith("data: ")]
        kinds = [e["type"] for e in events]
        self.assertGreaterEqual(kinds.count("response.in_progress"), 3)
        self.assertEqual(kinds[-1], "response.completed")
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))

    def test_private_log_records_metadata_only(self):
        self.sdk.results = ['{"output":[{"type":"message","text":"private-answer"}]}', UpstreamIncomplete()]
        self.post({"model": MODEL, "input": "private-prompt-text"})
        self.log_entries(1)
        self.post({"model": MODEL, "input": "private-prompt-text", "stream": True})
        self.log_entries(2)
        self.post({"model": "private-model-name", "input": "x"})
        entries = self.log_entries(3)
        path = self.logdir / "requests.jsonl"
        self.assertEqual(stat.S_IMODE(self.logdir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for private in ("private-prompt-text", "private-answer", "private-model-name"):
            self.assertNotIn(private, path.read_text())
        self.assertEqual([e["outcome"] for e in entries],
                         ["completed", "upstream_incomplete", "invalid_request:unsupported_request"])
        for entry in entries[:2]:
            self.assertEqual(entry["model"], MODEL)
            self.assertGreater(entry["prompt_bytes"], 0)
            self.assertGreaterEqual(entry["duration_s"], 0)
            self.assertIn("ts", entry)


    def test_log_includes_backend_queue_time(self):
        class QueuedSDK(StubSDK):
            async def generate(self, model, prompt):
                REQUEST_STATS.get()["queue_s"] = 1.5
                return OK
        self.service.backend = QueuedSDK()
        self.post({"model": MODEL, "input": "test"})
        self.assertEqual(self.log_entries(1)[-1]["queue_s"], 1.5)


    def test_single_code_fence_and_leading_note_are_recovered(self):
        fence, nl = "`" * 3, chr(10)
        self.sdk.results = [fence + "json" + nl + OK + nl + fence, "Checking the logs." + nl + nl + OK]
        texts = []
        for _ in range(2):
            _, events = self.post({"model": MODEL, "input": "test", "stream": True})
            self.assertEqual(events[-1]["type"], "response.completed")
            texts.append([item["content"][0]["text"] for item in events[-1]["response"]["output"]])
        self.assertEqual(texts, [["ok"], ["Checking the logs.", "ok"]])
        self.assertEqual([entry.get("output_repair") for entry in self.log_entries(2)], ["code_fence", "prose_prefix"])

    def test_text_after_json_or_broken_json_is_still_invalid(self):
        nl = chr(10)
        for result in ("Checking." + nl + OK + nl + "Done.", "Checking." + nl + OK[:-2]):
            with self.subTest(result=result):
                self.sdk.results = [result]
                _, error = self.failed({})
                self.assertEqual(error["code"], "invalid_model_output")


class PromptTests(unittest.TestCase):
    def test_prompt_ends_with_reminder_and_payload_roundtrips(self):
        history, prompt, _ = prepare_request({"model": MODEL, "input": "hello"})
        self.assertTrue(prompt.endswith(FORMAT_REMINDER))
        self.assertEqual(request_payload(prompt)["input"], history)


class RequestLogTests(unittest.TestCase):
    def test_rotation_bounds_size_and_file_count(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "logs"
            log = RequestLog(directory, max_bytes=300, backups=2)
            for index in range(40):
                log.record(outcome="completed", index=index)
            files = sorted(directory.iterdir())
            self.assertEqual([f.name for f in files], ["requests.jsonl", "requests.jsonl.1", "requests.jsonl.2"])
            for f in files:
                self.assertLessEqual(f.stat().st_size, 300)
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            self.assertEqual(json.loads(files[0].read_text().splitlines()[-1])["index"], 39)

    def test_error_codes(self):
        self.assertEqual(error_code("prompt_too_large"), "context_length_exceeded")
        self.assertEqual(error_code("deadline_expired"), "deadline_expired")
        self.assertEqual(failure_label(ValueError("secret")), "upstream_error:ValueError")


class BackendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.key = root / "key"
        self.key.write_text("synthetic-test-key")
        self.key.chmod(0o600)
        self.backend = SDKBackend(self.key, root / "workspace", timeouts={"high": 0.05, "xhigh": 5, "max": 5})
        self.backend.key = "synthetic-test-key"
        self.backend.workspace.mkdir()
        self.client = self.backend.client = StubClient()

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def test_per_effort_deadline_raises_deadline_expired_and_closes_bridge(self):
        async def slow_events():
            await asyncio.sleep(1)
            yield SimpleNamespace(kind="done", result_is_full=True)
        self.client.events = slow_events
        self.assertEqual(self.backend.deadline("claude-opus-5-5-xhigh"), 5)
        with self.assertRaises(DeadlineExpired):
            await self.backend.generate("claude-opus-5-5-high", "prompt")
        self.assertTrue(self.client.cancelled)
        self.assertTrue(self.client.closed)
        self.assertIsNone(self.backend.client)

    async def test_model_mismatch_label(self):
        async def wrong_model():
            return SimpleNamespace(status="finished", result="{}", model=SimpleNamespace(
                to_json=lambda: {"id": "claude-opus-5-5", "params": [{"id": "effort", "value": "max"}]}))
        self.client.wait = wrong_model
        with self.assertRaises(ModelMismatch):
            await self.backend.generate("claude-opus-5-5-high", "prompt")

    async def test_queue_wait_does_not_consume_inference_deadline(self):
        self.backend.timeouts["high"] = 0.5
        async def slow_events():
            await asyncio.sleep(0.3)
            yield SimpleNamespace(kind="result", result_is_full=True)
            yield SimpleNamespace(kind="done", result_is_full=False)
        self.client.events = slow_events
        stats = {}
        async def queued():
            REQUEST_STATS.set(stats)
            return await self.backend.generate("claude-opus-5-5-high", "prompt")
        first = asyncio.create_task(self.backend.generate("claude-opus-5-5-high", "prompt"))
        await asyncio.sleep(0)
        results = await asyncio.gather(first, queued())
        self.assertEqual(results, ['{"output":[]}'] * 2)
        self.assertGreaterEqual(stats["queue_s"], 0.2)

    async def test_queue_timeout_label(self):
        self.backend.queue_timeout = 0.05
        await self.backend.lock.acquire()
        try:
            with self.assertRaises(QueueTimeout):
                await self.backend.generate("claude-opus-5-5-xhigh", "prompt")
        finally:
            self.backend.lock.release()

    async def test_public_key_file_is_key_invalid(self):
        self.backend.client = None
        self.key.chmod(0o644)
        with self.assertRaises(KeyInvalid):
            await self.backend.generate("claude-opus-5-5-high", "prompt")


if __name__ == "__main__":
    unittest.main()
