import json
from pathlib import Path
import subprocess
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from jsonschema import ValidationError

from cursor_bridge.spike_tools import DeadlineExpired, FIXTURES, MODEL, deadline, exercise, managed_client, model_identity, parse_output, resolve_model, run_live, strict_json, summarize, wait_with_diagnostics


FIXTURE = strict_json(FIXTURES.read_text())


def envelope(expected):
    output = []
    for index, item in enumerate(expected):
        item = dict(item)
        if item["type"] == "function_call":
            item["call_id"] = f"call_{index}"
            item["arguments"] = json.dumps(item["arguments"])
        output.append(item)
    return json.dumps({"output": output})


class SpikeTests(unittest.TestCase):
    def test_terminal_diagnostics_preserve_failure_phase_and_do_not_accept_partial_stream(self):
        def unfinished_stream():
            yield SimpleNamespace(kind='result', result_is_full=True, sdk_message=None)
            raise DeadlineExpired('stub stream did not end')
        run = SimpleNamespace(events=unfinished_stream, wait=MagicMock())
        with self.assertRaises(DeadlineExpired) as caught:
            wait_with_diagnostics(run)
        self.assertTrue(caught.exception.spike_diagnostics['full_result_seen'])
        self.assertFalse(caught.exception.spike_diagnostics['done_seen'])
        self.assertEqual(caught.exception.spike_diagnostics['phase'], 'stream')
        run.wait.assert_not_called()

        run.events = lambda: iter([])
        run.wait.side_effect = TimeoutError('stub terminal wait failed')
        with self.assertRaises(TimeoutError) as caught:
            wait_with_diagnostics(run)
        self.assertEqual(caught.exception.spike_diagnostics['phase'], 'terminal_wait')

    def test_status_errors_are_classified_without_printing_messages(self):
        message = SimpleNamespace(type='status', message='Model not available; not supported in your region. secret-value')
        run = SimpleNamespace(events=lambda: iter([SimpleNamespace(sdk_message=message)]),
                              wait=lambda: SimpleNamespace(status='error', result='', model=None))
        result = wait_with_diagnostics(run)
        self.assertEqual(result.sdk_error_flags, ['model_unavailable', 'region_unsupported'])
        self.assertNotIn('secret-value', repr(result))

    def test_terminal_sdk_error_stops_remaining_inference(self):
        for transport_failure in (False, True):
            with self.subTest(transport_failure=transport_failure):
                client = MagicMock()
                client.models.list.return_value = [SimpleNamespace(id=MODEL)]
                run = client.agents.create.return_value.send.return_value
                if transport_failure:
                    run.events.side_effect = TimeoutError('stub transport failure')
                else:
                    run.events.return_value = iter([])
                    run.wait.return_value = SimpleNamespace(status='error', result='', model=None)
                with patch('cursor_bridge.spike_tools.managed_client') as managed:
                    managed.return_value.__enter__.return_value = client
                    report = run_live('direct', 'stub-key', 3, FIXTURE)
                self.assertEqual(client.agents.create.call_count, 1)
                self.assertEqual(len(report['results']), 1)
                self.assertIn('blocked', report)

    def test_resolves_high_effort_without_using_medium_default(self):
        from cursor_sdk.types import SDKModel
        def variant(effort, default=False):
            return {'params':[{'id':'context','value':'1m'},{'id':'effort','value':effort},{'id':'fast','value':'false'}], 'isDefault':default}
        model = SDKModel.from_json({'id':'claude-opus-5-5','displayName':'Opus 5.5',
                                    'variants':[variant('medium',True),variant('high')]})
        selected = resolve_model([model])
        self.assertEqual(model_identity(selected), ('claude-opus-5-5',{'context':'1m','effort':'high','fast':'false'}))
        missing = SDKModel.from_json({'id':'claude-opus-5-5','displayName':'Opus 5.5', 'variants':[variant('medium',True)]})
        with self.assertRaises(ValueError): resolve_model([missing])

    def test_every_fixture_is_valid_and_roundtrip_keeps_call_id(self):
        with tempfile.TemporaryDirectory() as directory:
            for case in FIXTURE["cases"]:
                prompts = []
                def send(prompt, observe):
                    prompts.append(prompt)
                    expected = case["expected"] if len(prompts) == 1 else case["followup"]["expected"]
                    observe(SimpleNamespace(type="text-delta"))
                    return SimpleNamespace(status="finished", result=envelope(expected))
                result = exercise(send, Path(directory), case, FIXTURE["tools"])
                self.assertTrue(all(row["passed"] for row in result), result)
                if case.get("followup"):
                    payload = json.loads(prompts[1].split('\n')[0])
                    self.assertEqual(payload, {"type": "function_call_output", "call_id": "call_0", "output": "spike_ok"})

    def test_rejects_malformed_and_invalid_calls_without_repair(self):
        valid = envelope(FIXTURE["cases"][0]["expected"])
        invalid = ["```json\n" + valid + "\n```", '{"output":[],"output":[]}', '{"output":[]}',
                   valid.replace('exec_command', 'unknown_tool'), valid.replace('call_0', '')]
        bad = json.loads(valid); bad["output"][0]["arguments"] = '{}'; invalid.append(json.dumps(bad))
        bad = json.loads(valid); bad["output"][0]["arguments"] = {"cmd": "pwd"}; invalid.append(json.dumps(bad))
        bad = json.loads(valid); bad["output"] *= 2; invalid.append(json.dumps(bad))
        bad = json.loads(valid); bad["output"][0]["arguments"] = '{"cmd":"pwd","sandbox_permissions":"invalid"}'; invalid.append(json.dumps(bad))
        for value in invalid:
            with self.assertRaises((ValueError, ValidationError)):
                parse_output(value, FIXTURE["tools"])
        with self.assertRaises(ValueError):
            strict_json('{"x": NaN}')

    def test_native_tool_callbacks_and_workspace_changes_fail(self):
        case = FIXTURE["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for event_type in ['toolCall', 'tool-call-started', 'tool-call-completed', 'partial-tool-call']:
                def send(prompt, observe):
                    observe(SimpleNamespace(type=event_type))
                    return SimpleNamespace(status="finished", result=envelope(case["expected"]))
                result = exercise(send, workspace, case, FIXTURE["tools"])[0]
                self.assertFalse(result["passed"])
                self.assertEqual(result["native_tool_events"], {event_type: 1})
            def write_file(prompt, observe):
                (workspace / 'unexpected').write_text('marker')
                return SimpleNamespace(status="finished", result=envelope(case["expected"]))
            result = exercise(write_file, workspace, case, FIXTURE["tools"])[0]
            self.assertFalse(result["passed"])
            self.assertEqual(result["changed_paths"], ['unexpected'])

    def test_sdk_failure_wrong_semantics_and_secret_redaction(self):
        case = FIXTURE["cases"][0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for status, output in [('error', envelope(case['expected'])), ('finished', ''),
                                   ('finished', envelope([{'type':'message','text':'wrong'}]))]:
                result = exercise(lambda *_: SimpleNamespace(status=status,result=output), workspace, case, FIXTURE['tools'])[0]
                self.assertFalse(result['passed'])
            result = exercise(lambda *_: SimpleNamespace(status='finished',result='secret-value'), workspace, case, FIXTURE['tools'],key='secret-value')[0]
            self.assertNotIn('secret-value', json.dumps(result))
            def fail(*_):
                raise RuntimeError('secret-value')
            result = exercise(fail, workspace, case, FIXTURE['tools'],key='secret-value')[0]
            self.assertNotIn('secret-value', json.dumps(result))

    def test_incomplete_or_stub_reports_cannot_pass(self):
        report = summarize({'results':[{'passed':True}], 'live_inference':True},24)
        self.assertFalse(report['tool_probe_passed'])
        self.assertEqual(report['first_pass_rate'],1/24)
        report = summarize({'results':[{'passed':True}]*24,'live_inference':False},24)
        self.assertFalse(report['tool_probe_passed'])

    def test_heartbeats_do_not_extend_wall_clock_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            def heartbeat_forever(prompt, observe):
                with deadline(0.04):
                    while True:
                        observe(SimpleNamespace(type='heartbeat'))
                        time.sleep(0.002)
            started = time.monotonic()
            result = exercise(heartbeat_forever, Path(directory), FIXTURE['cases'][0], FIXTURE['tools'])[0]
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(result['passed'])
            self.assertEqual(result['error_type'], 'DeadlineExpired')

    def test_wall_clock_deadline_is_not_reclassified_as_http_read_timeout(self):
        from httpcore._backends.sync import SyncStream

        reader, writer = socket.socketpair()
        try:
            with self.assertRaises(DeadlineExpired):
                with deadline(0.04):
                    SyncStream(reader).read(1, timeout=10)
        finally:
            reader.close()
            writer.close()

    def test_cleanup_targets_owned_process_even_if_sdk_close_fails(self):
        client = MagicMock()
        process = client._owned_bridge.process
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('stub', 5), 0]
        client.close.side_effect = DeadlineExpired('stub timeout')
        with patch('cursor_sdk.CursorClient.launch_bridge', return_value=client):
            with self.assertRaises(DeadlineExpired):
                with managed_client(['stub'], '/tmp/unused-workspace', '/tmp/unused-state'):
                    pass
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()

    def test_cleanup_errors_preserve_the_original_failure(self):
        client = MagicMock()
        client.close.side_effect = DeadlineExpired('stub close error')
        client._owned_bridge.process.poll.return_value = 0
        with patch('cursor_sdk.CursorClient.launch_bridge', return_value=client):
            with self.assertRaises(ValueError) as caught:
                with managed_client(['stub'], '/tmp/unused-workspace', '/tmp/unused-state'):
                    raise ValueError('stub catalog failure')
        cleanup = caught.exception.spike_cleanup_errors[0]
        self.assertEqual(cleanup['error_type'], 'DeadlineExpired')
        self.assertEqual(cleanup['failed_operation'], 'bridge_close')

    def test_full_stub_pipeline_counts_turns_and_disables_native_tools(self):
        from cursor_sdk import ModelSelection

        for shared in (False, True):
            with self.subTest(shared=shared):
                client = MagicMock()
                client.models.list.return_value = [SimpleNamespace(id=MODEL)]
                cases = iter(FIXTURE['cases'] * 3)
                workspaces = []
                def create(options):
                    self.assertEqual(options.tools, [])
                    self.assertEqual(options.local.setting_sources, [])
                    self.assertEqual(options.disallowed_tools, ['mcp', 'task', 'shell'])
                    workspaces.append(options.local.cwd)
                    case = next(cases)
                    agent = MagicMock()
                    turns = 0
                    def send(prompt, settings):
                        nonlocal turns
                        turns += 1
                        settings.on_delta(SimpleNamespace(type='text-delta'))
                        expected = case['expected'] if turns == 1 else case['followup']['expected']
                        result = SimpleNamespace(status='finished', result=envelope(expected), model=ModelSelection.from_value(MODEL))
                        return SimpleNamespace(events=lambda: iter([]), wait=lambda: result)
                    agent.send.side_effect = send
                    return agent
                client.agents.create.side_effect = create
                with patch('cursor_bridge.spike_tools.managed_client') as managed:
                    managed.return_value.__enter__.return_value = client
                    report = run_live('direct', 'stub-key', 3, FIXTURE, shared_workspace=shared)
                self.assertNotIn('error_type', report)
                self.assertEqual(len(report['results']), 24)
                self.assertTrue(all(item['passed'] for item in report['results']))
                self.assertEqual(client.agents.create.call_count, 21)
                self.assertEqual(len(set(workspaces)), 1 if shared else 21)


if __name__ == '__main__':
    unittest.main()
