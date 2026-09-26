import asyncio
from types import SimpleNamespace
import unittest

from cursor_sdk_bridge.probe_native import PendingProbe, run_probe


class CallbackContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_callback_server_survives_http_boundary(self):
        receipt = await run_probe(timeout=10)
        self.assertTrue(receipt["success"])
        self.assertTrue(receipt["pending_survived_first_http_response"])
        self.assertTrue(receipt["second_http_response_completed_same_run"])
        self.assertTrue(receipt["result_content_shape_preserved"])
        self.assertEqual(receipt["parallel_pending_count"], 2)
        self.assertEqual(receipt["callback_execution_count"], 2)
        self.assertEqual(receipt["upstream_inference_calls"], 0)

    async def test_mismatched_batch_does_not_resolve_pending(self):
        async def driver(probe):
            return await probe.execute({}, SimpleNamespace(tool_call_id="a"))
        probe = PendingProbe(driver, timeout=2)
        try:
            first = await probe.first()
            with self.assertRaises(ValueError):
                await probe.continuation({"wrong": "result"})
            self.assertFalse(probe.pending["a"].done())
            second = await probe.continuation({first["call_ids"][0]: {"ok": True}})
            self.assertEqual(second["metadata"], {"ok": True})
            with self.assertRaises(ValueError):
                await probe.continuation({"a": "duplicate"})
        finally:
            await probe.close()

    async def test_run_failure_before_callback_does_not_wait_for_timeout(self):
        async def fail(probe):
            raise RuntimeError("synthetic failure")
        probe = PendingProbe(fail, timeout=30)
        try:
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(probe.first(), 1)
        finally:
            await probe.close()


if __name__ == "__main__":
    unittest.main()
