import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cursor_bridge.cursor_sdk2api import Service, make_server
from cursor_bridge.probe_service import ProbeFailed, probe
from cursor_bridge.responses_protocol import request_payload
from cursor_bridge.spike_tools import proxychains_binary


class EchoSDK:
    def __init__(self, fail=False, ignore_result=False):
        self.calls = 0
        self.fail = fail
        self.ignore_result = ignore_result

    async def generate(self, model, prompt):
        self.calls += 1
        if self.fail:
            raise RuntimeError('private upstream text must not escape')
        payload = request_payload(prompt)
        if self.calls == 1:
            assert payload['tool_choice']['name'] == 'fallback_probe.echo'
            item = {'type': 'function_call', 'name': 'fallback_probe.echo', 'call_id': 'echo-1',
                    'arguments': json.dumps({'value': 'FALLBACK_PROBE_OK_request'})}
        else:
            assert payload['input'][-1]['call_id'] == 'echo-1'
            assert payload['tool_choice'] == 'none'
            item = {'type': 'message', 'text': 'FALLBACK_PROBE_OK_request' if self.ignore_result else payload['input'][-1]['output']}
        return json.dumps({'output': [item]})

    async def close(self):
        pass


class PreflightTests(unittest.TestCase):
    def run_probe(self, sdk):
        service = Service(sdk)
        server = make_server(service, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch('cursor_bridge.probe_service.secrets.token_hex', side_effect=['request', 'result']):
                return probe(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
            service.close()

    def test_two_outputs_require_actual_namespace_and_result_roundtrip(self):
        sdk = EchoSDK()
        self.assertTrue(self.run_probe(sdk)['namespace_roundtrip'])
        self.assertEqual(sdk.calls, 2)

    def test_failed_sdk_is_not_retried_and_private_message_is_hidden(self):
        sdk = EchoSDK(fail=True)
        with self.assertRaises(ProbeFailed) as raised:
            self.run_probe(sdk)
        self.assertNotIn('private', str(raised.exception))
        self.assertEqual(sdk.calls, 1)

    def test_model_that_ignores_tool_result_cannot_pass(self):
        with self.assertRaisesRegex(ProbeFailed, 'did not consume'):
            self.run_probe(EchoSDK(ignore_result=True))


class ProxyResolutionTests(unittest.TestCase):
    def test_user_wrapper_precedes_system_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            wrapper = home / '.local/bin/proxychains4'
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('#!/bin/sh\nexit 0\n')
            wrapper.chmod(0o700)
            with patch('cursor_bridge.spike_tools.Path.home', return_value=home), patch.dict('os.environ', {'CURSOR_FALLBACK_PROXYCHAINS': ''}), patch('cursor_bridge.spike_tools.shutil.which', return_value='/usr/bin/proxychains4'):
                self.assertEqual(proxychains_binary(), str(wrapper))

    def test_explicit_relative_or_nonexecutable_proxy_is_rejected(self):
        for value in ('relative-proxy', '/no-such-fallback-proxy'):
            with patch.dict('os.environ', {'CURSOR_FALLBACK_PROXYCHAINS': value}):
                with self.assertRaises(RuntimeError):
                    proxychains_binary()


if __name__ == '__main__':
    unittest.main()
