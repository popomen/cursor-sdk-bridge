import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.failures import DeadlineExpired, QueueTimeout
from cursor_sdk_bridge.sdk_backend import SDKBackend, Slots
from cursor_sdk_bridge.switch_config import service_report
from test_cursor_sdk2api import StubSDK
from test_sdk_backend import StubClient

MAX = "claude-opus-5-5-max"
IDLE = {"queued": 0, "active": None, "runs": []}


class ProgressSDK(StubSDK):
    snapshot = {"queued": 1, "active": {"model": MAX, "running_s": 812.4, "events": 1650,
                                        "idle_s": 0.6, "deadline_s": 1200}}

    def progress(self):
        return self.snapshot


class BackendProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        key = root / "key"
        key.write_text("synthetic-test-key")
        key.chmod(0o600)
        self.backend = SDKBackend(key, root / "workspace", timeout=5, max_concurrency=2)
        self.backend.key = "synthetic-test-key"
        self.backend.workspace.mkdir()
        self.client = self.backend.client = StubClient()

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def test_reports_running_inference_and_queue_as_metadata_only(self):
        release = asyncio.Event()
        async def events():
            yield SimpleNamespace(kind="result", result_is_full=True)
            yield SimpleNamespace(kind="thinking", result_is_full=False)
            await release.wait()
            yield SimpleNamespace(kind="done", result_is_full=False)
        self.client.events = events
        self.assertEqual(self.backend.progress(), IDLE)
        # The stub shares one agent, so every concurrent request uses one model.
        running = []
        for _ in range(3):
            running.append(asyncio.create_task(self.backend.generate(MAX, "private-prompt")))
            await asyncio.sleep(0.01)
        for _ in range(200):
            await asyncio.sleep(0.01)
            report = self.backend.progress()
            if report["queued"] == 1 and [run["events"] for run in report["runs"]] == [2, 2]:
                break
        self.assertEqual(report["queued"], 1)
        self.assertEqual([run["model"] for run in report["runs"]], [MAX, MAX])
        self.assertEqual(report["active"], report["runs"][0])
        self.assertGreaterEqual(report["runs"][0]["running_s"], report["runs"][1]["running_s"])
        self.assertEqual(report["active"]["deadline_s"], 5)
        self.assertGreaterEqual(report["active"]["running_s"], report["active"]["idle_s"])
        self.assertNotIn("private-prompt", json.dumps(report))
        release.set()
        self.assertEqual(await asyncio.gather(*running), ['{"output":[]}'] * 3)
        self.assertEqual(self.backend.progress(), IDLE)

    async def test_failures_clear_active_inference_and_queue(self):
        self.backend.timeouts["high"] = 0.05
        async def slow_events():
            await asyncio.sleep(1)
            yield SimpleNamespace(kind="done", result_is_full=True)
        self.client.events = slow_events
        with self.assertRaises(DeadlineExpired):
            await self.backend.generate("claude-opus-5-5-high", "prompt")
        self.assertEqual(self.backend.progress(), IDLE)
        self.backend.queue_timeout = 0.05
        self.backend.slots = Slots(0)
        with self.assertRaises(QueueTimeout):
            await self.backend.generate(MAX, "prompt")
        self.assertEqual(self.backend.progress(), IDLE)


class HealthProgressTests(unittest.TestCase):
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

    def health(self, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/health")
        health = json.load(connection.getresponse())
        connection.close()
        return health

    def test_health_and_status_expose_backend_progress(self):
        port = self.serve(ProgressSDK())
        health = self.health(port)
        self.assertIn("sdk_progress", health["capabilities"])
        self.assertEqual(health["progress"], ProgressSDK.snapshot)
        self.assertEqual(service_report(port)["progress"], ProgressSDK.snapshot)

    def test_backend_without_progress_reports_none(self):
        port = self.serve(StubSDK())
        self.assertIsNone(self.health(port)["progress"])
        self.assertIsNone(self.health(port)["limits"])
        self.assertNotIn("progress", service_report(port))

    def test_health_reports_sdk_limits(self):
        limits = {"deadlines": {"high": 1200, "xhigh": 1200, "max": 1800}, "queue_timeout": 1800,
                  "max_concurrency": 5}
        with tempfile.TemporaryDirectory() as temp:
            backend = SDKBackend(Path(temp) / "key", Path(temp) / "workspace", timeouts=limits["deadlines"],
                                 queue_timeout=limits["queue_timeout"], max_concurrency=5)
            self.assertEqual(self.health(self.serve(backend))["limits"], limits)


if __name__ == "__main__":
    unittest.main()
