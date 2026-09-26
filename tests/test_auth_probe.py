from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cursor_bridge.failures import KeyInvalid
from cursor_bridge.request_log import REQUEST_STATS
from cursor_bridge.sdk_backend import SDKBackend


class Denied(Exception):
    status_code = 401
    code = "unauthenticated"


class AuthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_denial_is_probed_without_retrying_inference(self):
        for result, verdict in ((None, "valid"), (Denied("private"), "invalid"), (OSError("private"), "unknown")):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as directory:
                calls, stats = [], {}
                async def me(**kwargs):
                    calls.append("me")
                    if result:
                        raise result
                backend = SDKBackend(Path(directory) / "key", Path(directory))
                backend.key = "synthetic"
                backend.client = SimpleNamespace(me=me)
                token = REQUEST_STATS.set(stats)
                try:
                    if verdict == "invalid":
                        with self.assertRaises(KeyInvalid):
                            await backend._probe_auth_failure(Denied("private"))
                    else:
                        await backend._probe_auth_failure(Denied("private"))
                finally:
                    REQUEST_STATS.reset(token)
                self.assertEqual(calls, ["me"])
                self.assertEqual(stats, {"credential_probe": verdict})

    async def test_non_auth_failure_does_not_probe(self):
        backend = SDKBackend("unused", "unused")
        backend.client = SimpleNamespace()
        await backend._probe_auth_failure(RuntimeError("private"))
