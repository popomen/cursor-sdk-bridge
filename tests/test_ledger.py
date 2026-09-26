import asyncio
import http.client
import json
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.ledger import ResultLedger, request_digest

MODEL = "claude-opus-5-5-high"
BODY = {"model": MODEL, "input": "synthetic private input"}


class GateBackend:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.cancelled = False

    async def generate(self, model, prompt):
        self.calls += 1
        self.started.set()
        try:
            while not self.release.is_set():
                await asyncio.sleep(.005)
            return '{"output":[{"type":"message","text":"synthetic private answer"}]}'
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        pass


class LedgerTests(unittest.TestCase):
    def test_ttl_private_file_and_unchanged_parent_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            now = [100]
            ledger = ResultLedger(root / "results.sqlite3", ttl=5, clock=lambda: now[0])
            ledger.put("hash", {"output": []}, {"usage": {"input_tokens": 9}, "unsafe": "secret"})
            self.assertNotIn("unsafe", ledger.get("hash")["stats"])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(ledger.path.stat().st_mode), 0o600)
            now[0] = 106
            self.assertIsNone(ledger.get("hash"))
            ledger.close()

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target, link = Path(directory) / "target", Path(directory) / "link"
            target.write_text("unchanged")
            link.symlink_to(target)
            with self.assertRaises(OSError):
                ResultLedger(link)
            self.assertEqual(target.read_text(), "unchanged")

    def test_digest_ignores_transport_but_keeps_semantics(self):
        h = [{"role": "user", "content": "hello"}]
        first = request_digest(BODY, h, "native")
        self.assertEqual(first, request_digest({**BODY, "stream": True, "store": False}, h, "native"))
        self.assertNotEqual(first, request_digest({**BODY, "instructions": "different"}, h, "native"))
        self.assertNotEqual(first, request_digest(BODY, h, "legacy"))


class DetachedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "results.sqlite3"
        self.backend = GateBackend()
        self.service = Service(self.backend, ledger_path=self.path)
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def submit(self, body=BODY, stats=None):
        shell, history, prompt, images = self.service.prepare(body)
        future = self.service.submit(shell, body, history, prompt, stats, images)
        return future, shell

    def request(self, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        status, content = response.status, json.loads(response.read())
        connection.close()
        return status, content

    def test_concurrent_retry_joins_same_future_and_response_id(self):
        first, a = self.submit()
        second, b = self.submit()
        self.assertIs(first.live_output, second.live_output)
        self.assertEqual(a["id"], b["id"])
        self.backend.release.set()
        self.assertEqual(first.result(3)["id"], a["id"])
        self.assertEqual(self.backend.calls, 1)

    def test_joining_store_true_retains_history_after_store_false_owner(self):
        first, _ = self.submit({**BODY, "store": False})
        second, _ = self.submit({**BODY, "store": True})
        self.assertIs(first.live_output, second.live_output)
        self.backend.release.set()
        response = second.result(3)
        followup = {"model": MODEL, "previous_response_id": response["id"], "input": "next"}
        self.assertEqual(len(self.service.prepare(followup)[1]), 3)

    def test_completed_retry_survives_process_service_restart(self):
        self.backend.release.set()
        first, _ = self.submit()
        result = first.result(3)
        self.service.close()
        self.backend = GateBackend()
        self.service = Service(self.backend, ledger_path=self.path)
        self.server.service = self.service
        stats = {}
        second, shell = self.submit(stats=stats)
        self.assertEqual(second.result(3), result)
        self.assertEqual(shell["id"], result["id"])
        self.assertEqual(stats["dedup"], "hit")
        self.assertEqual(self.backend.calls, 0)
        followup = {"model": MODEL, "previous_response_id": result["id"], "input": "next"}
        self.assertEqual(len(self.service.prepare(followup)[1]), 3)

    def test_http_disconnect_does_not_cancel_and_result_is_retrievable(self):
        body = {**BODY, "stream": True}
        wire = json.dumps(body).encode()
        with patch("cursor_sdk_bridge.cursor_sdk2api.KEEPALIVE_SECONDS", .01):
            connection = socket.create_connection(("127.0.0.1", self.server.server_port))
            connection.sendall(b"POST /v1/responses HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: "
                               + str(len(wire)).encode() + b"\r\n\r\n" + wire)
            self.assertTrue(self.backend.started.wait(2))
            connection.recv(4096)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            connection.close()
            time.sleep(.05)
            self.assertEqual(self.service.lifecycle()["unfinished"], 1)
            self.assertFalse(self.backend.cancelled)
            self.backend.release.set()
            status, result = self.request("/v1/responses", BODY)
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(self.backend.calls, 1)

    def test_drain_blocks_new_admission_until_resumed(self):
        future, _ = self.submit()
        status, value = self.request("/admin/drain", {})
        self.assertEqual(status, 409)
        self.assertFalse(value["draining"])
        self.backend.release.set()
        future.result(3)
        status, value = self.request("/admin/drain", {})
        self.assertEqual((status, value["unfinished"]), (200, 0))
        with self.assertRaisesRegex(RuntimeError, "draining"):
            self.submit({**BODY, "input": "other"})
        self.request("/admin/resume", {})
        self.assertFalse(self.service.lifecycle()["draining"])

    def test_completed_stream_retry_reuses_created_id(self):
        self.backend.release.set()
        result = self.submit()[0].result(3)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("POST", "/v1/responses", json.dumps({**BODY, "stream": True}))
        response = connection.getresponse()
        events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
        connection.close()
        self.assertEqual(events[0]["response"]["id"], result["id"])
        self.assertEqual(events[-1]["response"]["id"], result["id"])
