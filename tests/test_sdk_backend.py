import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cursor_sdk_bridge.sdk_backend import SDKBackend
from test_models import VARIANTS, expected_selection


class StubClient:
    def __init__(self):
        self.agents = self
        self._owned_bridge = SimpleNamespace(process=SimpleNamespace(returncode=0, stderr=None),
            _tool_callback_server=None, _store_callback_server=None)
        self.options = self.send_options = None
        self.closed = self.cancelled = False
        self.native = False
        self.finished = self.full = self.done = True

    async def create(self, options):
        self.options = options
        return self

    async def send(self, prompt, options):
        self.send_options = options
        if self.native:
            options.on_step({"type": "toolCall"})
        return self

    async def events(self):
        yield SimpleNamespace(kind="result", result_is_full=self.full)
        if self.done:
            yield SimpleNamespace(kind="done", result_is_full=False)

    async def wait(self):
        return SimpleNamespace(status="finished" if self.finished else "error", result='{"output":[]}',
            model=SimpleNamespace(to_json=lambda: self.options.model))

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.returncode, self.stderr = None, None

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.key = root / "key"
        self.key.write_text("synthetic-test-key")
        self.key.chmod(0o600)
        self.backend = SDKBackend(self.key, root / "workspace", timeout=1)
        self.backend.key = self.key.read_text()
        self.backend.workspace.mkdir()
        self.client = StubClient()
        self.backend.client = self.client

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def test_disables_native_tools_and_maps_all_twenty_variants(self):
        for alias, context, effort, fast in VARIANTS:
            await self.backend.generate(alias, "prompt")
            options = self.client.options
            self.assertEqual(options.tools, [])
            self.assertEqual(options.disallowed_tools, ["mcp", "task", "shell"])
            self.assertEqual(options.local.setting_sources, [])
            self.assertEqual(options.model, expected_selection(context, effort, fast))

    async def test_requires_full_done_finished_terminal_and_no_native_tool_events(self):
        for field in ("full", "done", "finished", "native"):
            self.client = StubClient()
            setattr(self.client, field, field == "native")
            self.backend.client = self.client
            with self.assertRaises(RuntimeError):
                await self.backend.generate("claude-opus-5-5-high", "prompt")
            self.assertTrue(self.client.closed)

    async def test_cancel_during_launch_retains_process_ownership(self):
        self.backend.client = None
        launched = asyncio.Event()
        async def launch(**kwargs):
            launched.set()
            await asyncio.sleep(0.05)
            return self.client
        with patch("cursor_sdk.AsyncCursorClient.launch_bridge", side_effect=launch):
            task = asyncio.create_task(self.backend.generate("claude-opus-5-5-high", "prompt"))
            await launched.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.client.closed)
        self.assertIsNone(self.backend.client)


    async def test_concurrent_requests_share_one_launch_and_relaunch_after_exit(self):
        self.backend.client, clients = None, []
        async def launch(**kwargs):
            await asyncio.sleep(0.02)
            client = StubClient()
            client._owned_bridge.process, client.shutdowns = FakeProcess(), 0
            async def aclose(client=client):
                client.shutdowns += 1
            client.aclose = aclose
            clients.append(client)
            return client
        with patch("cursor_sdk.AsyncCursorClient.launch_bridge", side_effect=launch):
            results = await asyncio.gather(*(self.backend.generate("claude-opus-5-5-high", "prompt")
                                             for _ in range(3)))
            self.assertEqual(results, ['{"output":[]}'] * 3)
            self.assertEqual(len(clients), 1)
            clients[0]._owned_bridge.process.returncode = 1
            await self.backend.generate("claude-opus-5-5-high", "prompt")
        self.assertEqual(len(clients), 2)
        self.assertEqual([client.shutdowns for client in clients], [1, 0])
        self.assertIs(self.backend.client, clients[1])
        self.assertEqual(self.backend.retiring, [])

if __name__ == "__main__":
    unittest.main()
