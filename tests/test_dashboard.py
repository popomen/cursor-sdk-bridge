import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse

from cursor_bridge import claude_switch
from cursor_bridge import dashboard
from cursor_bridge.cursor_sdk2api import Service, make_server
from cursor_bridge.request_log import REQUEST_STATS, RequestLog
from cursor_bridge.sdk_backend import DEFAULT_QUEUE_TIMEOUT, DEFAULT_TIMEOUTS, SDKBackend

NL = chr(10)
NOW = datetime.now().astimezone()
MAX = 'claude-opus-5-5-max'
SPEC = dashboard.INSTANCES[1]
CURRENT = {'service': 'cursor-sdk2api', 'status': 'ready', 'adapter_version': 3, 'running_version': 'current-commit',
           'capabilities': list(dashboard.REQUIRED_CAPABILITIES), 'progress': {'queued': 0, 'active': None},
           'limits': {'deadlines': {'high': 1200.0, 'xhigh': 1200.0, 'max': 1800.0}, 'queue_timeout': 1800.0}}
LEGACY = {'service': 'cursor-sdk2api', 'status': 'ready', 'adapter_version': 3, 'capabilities': ['image_inputs']}
UNIT = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '42', 'NRestarts': '0',
        'ActiveEnterTimestamp': 'Sat 2026-09-26 16:00:00 CST'}


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec='milliseconds')


def with_active(**active):
    base = {'model': MAX, 'running_s': 100.0, 'events': 50, 'idle_s': 1.0, 'deadline_s': 1800.0}
    return {**CURRENT, 'progress': {'queued': 1, 'active': {**base, **active}}}


class ExpectationTests(unittest.TestCase):
    def test_expected_capabilities_and_limits_match_the_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(SDKBackend(Path(temp) / 'key', Path(temp) / 'workspace'))
            server = make_server(service, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                health, error = dashboard.fetch_health(server.server_port)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                service.close()
            self.assertEqual(dashboard.fetch_health(server.server_port), (None, 'URLError'))
        self.assertIsNone(error)
        self.assertLessEqual(set(dashboard.REQUIRED_CAPABILITIES), set(health['capabilities']))
        self.assertIsInstance(health['limits'], dict)
        codex, claude = dashboard.INSTANCES
        self.assertEqual((codex['port'], claude['port']), (8789, 8790))
        self.assertEqual(codex['limits'], {'max': DEFAULT_TIMEOUTS['max'], 'queue_timeout': DEFAULT_QUEUE_TIMEOUT})
        self.assertEqual(claude['limits'], {'max': claude_switch.MAX_DEADLINE_S,
                                            'queue_timeout': claude_switch.QUEUE_TIMEOUT_S})

class DescribeTests(unittest.TestCase):
    def test_states_and_notes(self):
        offline = dashboard.describe(SPEC, None, 'URLError')
        self.assertEqual((offline['state'], offline['error'], len(offline['notes'])), ('offline', 'URLError', 1))
        legacy = dashboard.describe(SPEC, LEGACY, None)
        self.assertEqual((legacy['state'], len(legacy['notes'])), ('legacy', 1))
        self.assertIn('sdk_progress', legacy['notes'][0])
        idle = dashboard.describe(SPEC, CURRENT, None)
        self.assertEqual((idle['state'], idle['notes'], idle['queued']), ('idle', [], 0))
        running = dashboard.describe(SPEC, with_active(), None)
        self.assertEqual((running['state'], len(running['notes'])), ('running', 1))
        self.assertIn('1 个请求在排队', running['notes'][0])
        stalled = dashboard.describe(SPEC, with_active(idle_s=300.0, running_s=1700.0), None)
        self.assertEqual((stalled['state'], len(stalled['notes'])), ('stalled', 3))
        old = {**CURRENT, 'limits': {'deadlines': {'high': 1200, 'xhigh': 1200, 'max': 1200}, 'queue_timeout': 1200}}
        self.assertIn('预期', dashboard.describe(SPEC, old, None)['notes'][0])


class SummaryTests(unittest.TestCase):
    def test_window_rejections_percentiles_and_tokens(self):
        entries = [
            {'ts': ts(60 * 30), 'model': MAX, 'outcome': 'completed', 'duration_s': 999.0},
            {'ts': ts(5), 'outcome': 'invalid_request:unsupported_request'},
            {'ts': ts(4), 'model': MAX, 'outcome': 'completed', 'duration_s': 10.0,
             'usage': {'input_tokens': 100, 'output_tokens': 7}},
            {'ts': ts(3), 'model': MAX, 'outcome': 'completed', 'duration_s': 30.0,
             'usage': {'input_tokens': 200, 'output_tokens': 3}},
            {'ts': ts(2), 'model': MAX, 'outcome': 'deadline_expired', 'duration_s': 1800.0},
            {'ts': ts(1), 'model': 'claude-opus-5-5-high', 'outcome': 'completed', 'duration_s': 5.0},
            {'ts': 'garbage'},
        ]
        summary = dashboard.summarize(entries, NOW)
        self.assertEqual((summary['requests'], summary['completed'], summary['rejected']), (4, 3, 1))
        self.assertEqual(summary['outcomes'], {'completed': 3, 'deadline_expired': 1})
        self.assertEqual(summary['tokens'], {'input': 300, 'output': 10})
        row = next(model for model in summary['models'] if model['model'] == MAX)
        self.assertEqual((row['requests'], row['completed'], row['p50_s'], row['p90_s'], row['max_s']),
                         (3, 2, 10.0, 30.0, 30.0))
        empty = dashboard.summarize([], NOW)
        self.assertEqual((empty['requests'], empty['completed'], empty['tokens']), (0, 0, None))

    def test_http_retries_and_disconnects_are_separate_from_inference_and_cache_usage(self):
        usage = {'input_tokens': 100, 'output_tokens': 10, 'cache_read_tokens': 80, 'cache_write_tokens': 20}
        common = {'ts': ts(1), 'model': MAX, 'outcome': 'completed', 'duration_s': 4, 'usage': usage}
        entries = [{**common, 'event': 'inference'}, {**common, 'event': 'http'},
                   {**common, 'event': 'http', 'dedup': 'hit'}, {**common, 'event': 'http', 'dedup': 'joined'},
                   {**common, 'event': 'http', 'outcome': 'client_disconnected'},
                   {'ts': ts(1), 'event': 'http', 'outcome': 'invalid_request:malformed_request'},
                   {**common, 'event': 'unknown_event'}]
        summary = dashboard.summarize(entries, NOW)
        self.assertEqual((summary['requests'], summary['completed'], summary['rejected']), (1, 1, 1))
        self.assertEqual((summary['inference_records'], summary['legacy_records']), (1, 0))
        self.assertEqual(summary['counting_basis'], 'inference_jobs_plus_legacy_requests')
        self.assertEqual(summary['tokens'], {'input': 100, 'output': 10, 'cache_read': 80, 'cache_write': 20})
        self.assertEqual(summary['outcomes'], {'completed': 1})
        self.assertEqual(summary['http'], {'requests': 5, 'outcomes': {
            'completed': 3, 'client_disconnected': 1, 'invalid_request:malformed_request': 1},
            'dedup': {'hit': 1, 'joined': 1}})
        self.assertEqual(summary['models'][0]['requests'], 1)

    def test_mixed_legacy_logs_keep_original_counts_without_new_http_usage(self):
        common = {'ts': ts(1), 'model': MAX, 'outcome': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 10}}
        summary = dashboard.summarize([common, {**common, 'event': 'inference'}, {**common, 'event': 'http'},
                                      {'ts': ts(1), 'outcome': 'invalid_request:unsupported_request'}], NOW)
        self.assertEqual((summary['requests'], summary['inference_records'], summary['legacy_records']), (2, 1, 1))
        self.assertEqual(summary['tokens'], {'input': 200, 'output': 20})
        self.assertEqual(summary['rejected'], 1)
        self.assertEqual(summary['http']['requests'], 1)

    def test_http_only_rows_do_not_invent_inference_or_usage(self):
        summary = dashboard.summarize([{'ts': ts(1), 'event': 'http', 'model': MAX, 'outcome': 'completed',
                                       'dedup': 'hit', 'usage': {'input_tokens': 900, 'output_tokens': 9}}], NOW)
        self.assertEqual(summary['requests'], 0)
        self.assertIsNone(summary['tokens'])
        self.assertEqual(summary['models'], [])
        self.assertEqual(summary['http']['requests'], 1)

    def test_recent_rows_label_dedup_and_only_show_counted_usage(self):
        common = {'ts': ts(1), 'instance': 'codex', 'model': MAX, 'outcome': 'completed',
                  'usage': {'input_tokens': 123, 'output_tokens': 456}}
        page = dashboard.recent_table([{**common, 'event': 'inference'},
                                       {**common, 'event': 'http', 'dedup': 'hit'},
                                       {**common, 'event': 'http', 'dedup': 'joined'}])
        self.assertIn('HTTP（缓存命中）', page)
        self.assertIn('HTTP（共享推理）', page)
        self.assertEqual(page.count('<td>123</td>'), 1)
        self.assertEqual(page.count('<td>456</td>'), 1)


class LedgerMeasurementTests(unittest.TestCase):
    '''Exercise actual HTTP logging, deduplication and a detached inference with no SDK network.'''
    def setUp(self):
        class FakeBackend:
            def __init__(self):
                self.started, self.release = threading.Event(), threading.Event()
                self.calls, self.cancelled = 0, False

            async def generate(self, model, prompt):
                self.calls += 1
                self.started.set()
                try:
                    while not self.release.is_set():
                        await asyncio.sleep(.005)
                    REQUEST_STATS.get()['usage'] = {'input_tokens': 100, 'output_tokens': 10,
                                                   'cache_read_tokens': 80, 'cache_write_tokens': 20}
                    return '{"output":[{"type":"message","text":"synthetic answer"}]}'
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

            async def close(self):
                pass

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.logs, self.reader = root / 'logs', dashboard.LogReader()
        self.backend = FakeBackend()
        self.service = Service(self.backend, ledger_path=root / 'results.sqlite3', log=RequestLog(self.logs))
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.body = {'model': MAX, 'input': 'synthetic request'}

    def tearDown(self):
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def request(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('POST', '/v1/responses', json.dumps(self.body), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        value = json.load(response)
        self.assertEqual(response.status, 200)
        connection.close()
        return value

    def wait_logs(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            entries = self.reader.entries(self.logs)
            if predicate(entries):
                return entries
            time.sleep(.005)
        self.fail('expected synthetic HTTP/inference log boundary did not arrive')

    def assert_single_inference(self, entries, http_requests):
        summary = dashboard.summarize(entries, datetime.now().astimezone())
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual((summary['requests'], summary['completed'], summary['inference_records'], summary['legacy_records']),
                         (1, 1, 1, 0))
        self.assertEqual(summary['tokens'], {'input': 100, 'output': 10, 'cache_read': 80, 'cache_write': 20})
        self.assertEqual(summary['http']['requests'], http_requests)
        return summary

    def test_completed_http_retry_records_one_inference_and_one_token_charge(self):
        self.backend.release.set()
        first, replay = self.request(), self.request()
        self.assertEqual(first, replay)
        entries = self.wait_logs(lambda rows: len(rows) == 3)
        summary = self.assert_single_inference(entries, 2)
        self.assertEqual(summary['http']['dedup'], {'hit': 1})

    def test_disconnect_continue_join_and_completed_retry_do_not_duplicate_usage(self):
        wire = json.dumps({**self.body, 'stream': True}).encode()
        with patch('cursor_bridge.cursor_sdk2api.KEEPALIVE_SECONDS', .01):
            connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3)
            try:
                connection.sendall(b'POST /v1/responses HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: '
                                   + str(len(wire)).encode() + b'\r\n\r\n' + wire)
                self.assertTrue(self.backend.started.wait(2))
                connection.recv(4096)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            finally:
                connection.close()
            entries = self.wait_logs(lambda rows: any(row.get('outcome') == 'client_disconnected' for row in rows))
        pending = dashboard.summarize(entries, datetime.now().astimezone())
        self.assertEqual((pending['requests'], pending['http']['requests']), (0, 1))
        self.assertIsNone(pending['tokens'])
        self.assertFalse(self.backend.cancelled)
        joined = threading.Event()
        original_submit = self.service.submit
        with self.service.submit_lock:
            original_future = next(iter(self.service.jobs.values()))[0]

        def observe_join(*args, **kwargs):
            future = original_submit(*args, **kwargs)
            if getattr(future, 'live_output', None) is original_future.live_output and not future.done():
                joined.set()
            return future

        with patch.object(self.service, 'submit', side_effect=observe_join), ThreadPoolExecutor(max_workers=1) as executor:
            retry = executor.submit(self.request)
            try:
                observed = joined.wait(2)
            finally:
                self.backend.release.set()
            self.assertTrue(observed)
            retry.result(3)
        self.request()
        entries = self.wait_logs(lambda rows: len(rows) == 4)
        summary = self.assert_single_inference(entries, 3)
        self.assertEqual(summary['http']['outcomes']['client_disconnected'], 1)
        self.assertEqual(summary['http']['dedup'], {'joined': 1, 'hit': 1})


class LogReaderTests(unittest.TestCase):
    def test_rotated_files_are_read_oldest_first_and_bad_lines_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp)
            (logs / 'requests.jsonl.1').write_text(json.dumps({'ts': ts(3), 'outcome': 'old'}) + NL)
            (logs / 'requests.jsonl').write_text('not json' + NL + json.dumps({'ts': ts(1), 'outcome': 'new'}) + NL
                                                 + json.dumps([1]) + NL)
            reader = dashboard.LogReader()
            self.assertEqual([entry['outcome'] for entry in reader.entries(logs)], ['old', 'new'])
            with (logs / 'requests.jsonl').open('a') as handle:
                handle.write(json.dumps({'ts': ts(0), 'outcome': 'newer'}) + NL)
            self.assertEqual([entry['outcome'] for entry in reader.entries(logs)], ['old', 'new', 'newer'])
            self.assertEqual(reader.entries(logs / 'missing'), [])


class HelperTests(unittest.TestCase):
    def test_established_connections_count_only_the_local_port(self):
        header = '  sl  local_address rem_address   st tx_queue rx_queue'
        rows = ['   0: 0100007F:2255 00000000:0000 0A 00000000:00000000',
                '   1: 0100007F:2255 0100007F:C350 01 00000000:00000000',
                '   2: 0100007F:C351 0100007F:2255 01 00000000:00000000',
                '   3: 0100007F:1F90 0100007F:C352 01 00000000:00000000']
        with tempfile.TemporaryDirectory() as temp:
            table, missing = Path(temp) / 'tcp', Path(temp) / 'missing'
            table.write_text(NL.join([header] + rows) + NL)
            self.assertEqual(dashboard.established_connections(8789, (table, missing)), 1)
            self.assertEqual(dashboard.established_connections(8080, (table,)), 1)
            self.assertEqual(dashboard.established_connections(8790, (table,)), 0)
            self.assertIsNone(dashboard.established_connections(8789, (missing,)))

    def test_restart_version_compares_running_and_deployed_release(self):
        board = dashboard.Dashboard(health_reader=lambda port: ({**CURRENT, 'running_version': 'abc'}, None),
                                    unit_reader=lambda unit: UNIT, connection_counter=lambda port: 0,
                                    version_reader=lambda: 'abc')
        item = board.inspect(SPEC)
        self.assertFalse(item['needs_restart'])
        board.version_reader = lambda: 'def'
        item = board.inspect(SPEC)
        self.assertTrue(item['needs_restart'])
        self.assertEqual((item['running_version'], item['deployed_version']), ('abc', 'def'))

    def test_restart_blocker(self):
        idle = {'state': 'idle', 'active': None, 'queued': 0, 'connections': 0,
                'running_version': 'old-commit', 'deployed_version': 'new-commit'}
        cases = [(idle, None), ({**idle, 'active': {'model': MAX}}, '有推理'), ({**idle, 'queued': 2}, '2 个请求'),
                 ({**idle, 'connections': 3}, '3 个未结束'), ({'state': 'legacy', 'connections': 0}, '无法确认'),
                 ({'state': 'legacy', 'connections': None}, '无法确认'), ({'state': 'offline', 'connections': None}, '无法确认')]
        for item, expected in cases:
            with self.subTest(item=item):
                blocker = dashboard.restart_blocker(item)
                if expected is None:
                    self.assertIsNone(blocker)
                else:
                    self.assertIn(expected, blocker)


class RestartTests(unittest.TestCase):
    def board(self, health, connections=0):
        self.calls = []

        def runner(unit):
            self.calls.append(unit)
        spec = {**dashboard.INSTANCES[1], 'port': 2}
        return dashboard.Dashboard((spec,), health_reader=lambda port: health, unit_reader=lambda unit: None,
                                   connection_counter=lambda port: connections, restart_runner=runner,
                                   ready_wait_s=0.2, version_reader=lambda: CURRENT['running_version'],
                                   admin_client=lambda port, action: {'draining': action == 'drain', 'unfinished': 0})

    def test_restart_waits_for_health_and_reports_when_it_does_not_return(self):
        board = self.board((CURRENT, None))
        board.health_reader = lambda port: (None, 'URLError') if self.calls else (CURRENT, None)
        result = board.restart('claude')
        self.assertEqual((result['result'], self.calls), ('failed', ['cursor-bridge-claude.service']))
        self.assertIn('没有恢复', result['message'])

    def test_busy_instances_and_unknown_names_are_not_restarted(self):
        board = self.board((CURRENT, None), connections=1)
        self.assertEqual(board.restart('claude')['result'], 'refused')
        self.assertIsNone(board.restart('nope'))
        self.assertEqual(self.calls, [])

    def test_runner_failure_is_reported(self):
        board = self.board((CURRENT, None))
        board.restart_runner = lambda unit: 'systemctl 退出码 5'
        result = board.restart('claude')
        self.assertEqual(result['result'], 'failed')
        self.assertIn('退出码 5', result['message'])


    def test_pending_work_refuses_and_resumes_admission(self):
        board = self.board((CURRENT, None))
        actions = []
        def admin(port, action):
            actions.append(action)
            return {'draining': action == 'drain', 'unfinished': 1}
        board.admin_client = admin
        self.assertEqual(board.restart('claude')['result'], 'refused')
        self.assertEqual(actions, ['drain', 'resume'])
        self.assertEqual(self.calls, [])

    def test_connection_race_after_draining_refuses_and_resumes(self):
        board = self.board((CURRENT, None))
        counts = iter((0, 1))
        board.connection_counter = lambda port: next(counts)
        actions = []
        def admin(port, action):
            actions.append(action)
            return {'draining': action == 'drain', 'unfinished': 0}
        board.admin_client = admin
        self.assertEqual(board.restart('claude')['result'], 'refused')
        self.assertEqual(actions, ['drain', 'resume'])
        self.assertEqual(self.calls, [])

    def test_unavailable_drain_endpoint_never_restarts(self):
        board = self.board((CURRENT, None))
        actions = []
        def admin(port, action):
            actions.append(action)
            raise RuntimeError('synthetic')
        board.admin_client = admin
        self.assertEqual(board.restart('claude')['result'], 'refused')
        self.assertEqual(actions, ['drain', 'resume'])
        self.assertEqual(self.calls, [])

    def test_restart_requires_the_selected_release(self):
        board = self.board((CURRENT, None))
        board.version_reader = lambda: 'expected-commit'
        self.assertEqual(board.restart('claude')['result'], 'failed')
        self.assertEqual(self.calls, ['cursor-bridge-claude.service'])

    def test_missing_release_identity_refuses_before_draining(self):
        for missing in ('running', 'deployed'):
            with self.subTest(missing=missing):
                current = {**CURRENT, 'running_version': None} if missing == 'running' else CURRENT
                board = self.board((current, None))
                if missing == 'deployed':
                    board.version_reader = lambda: None
                actions = []
                board.admin_client = lambda port, action: actions.append(action)
                result = board.restart('claude')
                self.assertEqual(result['result'], 'refused')
                self.assertIn('无法确认', result['message'])
                self.assertEqual((self.calls, actions), ([], []))

    def test_success_does_not_resume_old_process(self):
        board = self.board((CURRENT, None))
        actions = []
        def admin(port, action):
            actions.append(action)
            return {'draining': True, 'unfinished': 0}
        board.admin_client = admin
        self.assertEqual(board.restart('claude')['result'], 'restarted')
        self.assertEqual(actions, ['drain'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        logs = root / 'claude/logs'
        logs.mkdir(parents=True)
        entries = [{'ts': ts(2), 'api': 'messages', 'model': MAX, 'stream': True, 'prompt_bytes': 204800,
                    'outcome': 'completed', 'duration_s': 42.0, 'queue_s': 0.0,
                    'usage': {'input_tokens': 50000, 'output_tokens': 1200}},
                   {'ts': ts(1), 'api': 'messages', 'model': '<script>alert(1)</script>', 'outcome': 'deadline_expired',
                    'duration_s': 1800.0},
                   {'ts': ts(0), 'outcome': 'invalid_request:unsupported_request'}]
        (logs / 'requests.jsonl').write_text(''.join(json.dumps(entry) + NL for entry in entries))
        (root / 'codex').mkdir()
        (root / 'codex/config.toml').write_text('model_provider = ' + json.dumps('cursor') + NL + 'model = '
                                                + json.dumps('claude-opus-5-5-high') + NL)
        instances = ({**dashboard.INSTANCES[0], 'port': 1, 'state_dir': root / 'codex-missing'},
                     {**dashboard.INSTANCES[1], 'port': 2, 'state_dir': root / 'claude'})
        healths = {1: ({**LEGACY, 'running_version': CURRENT['running_version']}, None), 2: (with_active(), None)}
        self.restarts = []
        self.board = dashboard.Dashboard(instances, codex_home=root / 'codex', claude_settings=root / 'missing.json',
                                         claude_state=root / 'state.json', health_reader=lambda port: healths[port],
                                         unit_reader=lambda unit: UNIT, connection_counter=lambda port: 0,
                                         restart_runner=self.restarts.append, ready_wait_s=0.2,
                                         version_reader=lambda: CURRENT['running_version'],
                                         admin_client=lambda port, action: {'draining': action == 'drain', 'unfinished': 0})
        self.server = dashboard.make_server(self.board, 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(stop)

    def get(self, path, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.request('GET', path, headers={'Host': host} if host else {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def post(self, fields, headers=None):
        body = urllib.parse.urlencode(fields)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.request('POST', '/restart', body,
                           {'Content-Type': 'application/x-www-form-urlencoded', **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def test_page_and_api_show_metadata_and_escape_values(self):
        status, headers, page = self.get('/')
        self.assertEqual(status, 200)
        self.assertIn('default-src', headers['Content-Security-Policy'])
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        for text in ('推理中', '旧版代码', '1 个请求在排队', '预检拒绝 1', '读取失败（ValueError）', 'PID 42',
                     '/restart?instance=codex', '暂不能重启：有推理正在进行'):
            self.assertIn(text, page)
        self.assertNotIn(self.board.token, page)
        status, _, body = self.get('/api/status')
        snapshot = json.loads(body)
        self.assertEqual([item['state'] for item in snapshot['instances']], ['legacy', 'running'])
        self.assertEqual([item['restart_blocker'] is None for item in snapshot['instances']], [True, False])
        self.assertEqual([entry['outcome'] for entry in snapshot['recent']], ['deadline_expired', 'completed'])
        self.assertEqual(snapshot['clients'][0]['provider'], 'cursor')
        self.assertEqual(snapshot['clients'][1]['error'], 'ValueError')
        self.assertNotIn(self.board.token, body)

    def test_status_recent_preserves_event_and_dedup_metadata(self):
        path = Path(self.board.instances[1]['state_dir']) / 'logs' / 'requests.jsonl'
        entry = {'ts': ts(0), 'event': 'http', 'dedup': 'hit', 'api': 'messages', 'model': MAX,
                 'outcome': 'completed', 'usage': {'input_tokens': 999, 'output_tokens': 99},
                 'private_extra': 'must not leave the log reader'}
        with path.open('a') as handle:
            handle.write(json.dumps(entry) + NL)
        snapshot = json.loads(self.get('/api/status')[2])
        recent = next(row for row in snapshot['recent'] if row.get('event') == 'http')
        self.assertEqual((recent['event'], recent['dedup']), ('http', 'hit'))
        self.assertNotIn('private_extra', recent)
        summary = snapshot['instances'][1]['summary']
        self.assertEqual(summary['tokens'], {'input': 50000, 'output': 1200})
        self.assertEqual(summary['http']['requests'], 1)
        self.assertIn('HTTP（缓存命中）', self.get('/')[2])

    def test_foreign_host_and_unknown_path_are_refused(self):
        self.assertEqual(self.get('/', 'evil.example:8791')[0], 403)
        self.assertEqual(self.get('/api/status', 'localhost:9999')[0], 200)
        self.assertEqual(self.get('/nope')[0], 404)

    def test_confirm_page_offers_the_tokened_form_only_while_idle(self):
        status, headers, page = self.get('/restart?instance=codex')
        self.assertEqual(status, 200)
        self.assertIn('form-action', headers['Content-Security-Policy'])
        self.assertIn(self.board.token, page)
        self.assertIn('确认重启 cursor-bridge-codex.service', page)
        status, _, page = self.get('/restart?instance=claude')
        self.assertEqual(status, 200)
        self.assertNotIn(self.board.token, page)
        self.assertIn('有推理正在进行，现在不能重启', page)
        self.assertEqual(self.get('/restart?instance=nope')[0], 404)
        self.assertEqual(self.get('/restart?instance=codex', 'evil.example')[0], 403)

    def test_restart_post_needs_the_token_and_a_local_origin(self):
        good = {'token': self.board.token, 'instance': 'codex'}
        cases = [({'instance': 'codex'}, {}, 403), ({**good, 'token': 'wrong'}, {}, 403),
                 (good, {'Origin': 'https://evil.example'}, 403), (good, {'Origin': 'null'}, 403),
                 (good, {'Sec-Fetch-Site': 'cross-site'}, 403), (good, {'Host': 'evil.example:8791'}, 403),
                 (good, {'Content-Type': 'text/plain'}, 400), ({**good, 'instance': 'nope'}, {}, 404)]
        for fields, headers, status in cases:
            with self.subTest(fields=fields, headers=headers):
                self.assertEqual(self.post(fields, headers)[0], status)
        self.assertEqual(self.restarts, [])
        status, headers, _ = self.post(good, {'Origin': 'http://localhost:64517', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual((status, headers['Location']), (303, '/'))
        self.assertEqual(self.restarts, ['cursor-bridge-codex.service'])
        self.assertIn('已重启 cursor-bridge-codex.service，/health 已恢复。', self.get('/')[2])
        self.assertEqual(self.post({**good, 'instance': 'claude'})[0], 303)
        self.assertEqual(self.restarts, ['cursor-bridge-codex.service'])
        snapshot = json.loads(self.get('/api/status')[2])
        self.assertEqual((snapshot['last_action']['instance'], snapshot['last_action']['result']), ('claude', 'refused'))


if __name__ == '__main__':
    unittest.main()
