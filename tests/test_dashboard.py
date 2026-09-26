from datetime import datetime, timedelta
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.parse

from cursor_bridge import claude_switch
from cursor_bridge import dashboard
from cursor_bridge.cursor_sdk2api import Service, make_server
from cursor_bridge.sdk_backend import DEFAULT_QUEUE_TIMEOUT, DEFAULT_TIMEOUTS, SDKBackend

NL = chr(10)
NOW = datetime.now().astimezone()
MAX = 'claude-opus-5-5-max'
SPEC = dashboard.INSTANCES[1]
CURRENT = {'service': 'cursor-sdk2api', 'status': 'ready', 'adapter_version': 3,
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

    def test_runtime_files_exist_and_launchers_are_per_instance(self):
        launchers = [spec['launchers'] for spec in dashboard.INSTANCES]
        self.assertEqual(launchers, [(), ('serve_claude.sh',)])
        names = dashboard.RUNTIME_FILES + sum(launchers, ())
        self.assertEqual([name for name in names if not (dashboard.SCRIPTS / name).exists()], [])


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

    def test_stale_sources_lists_files_changed_after_the_start(self):
        with tempfile.TemporaryDirectory() as temp:
            started = time.time() - 100
            for name, mtime in (('old.py', started - 50), ('new.py', started + 50)):
                path = Path(temp) / name
                path.write_text('x')
                os.utime(path, (mtime, mtime))
            self.assertEqual(dashboard.stale_sources(started, temp, ('old.py', 'new.py', 'gone.py')), ['new.py'])
            self.assertEqual(dashboard.stale_sources(None, temp, ('new.py',)), [])

    def test_launcher_changes_only_mark_their_own_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'serve_claude.sh').write_text('x')
            unit = {**UNIT, 'started_at': time.time() - 100}
            board = dashboard.Dashboard(health_reader=lambda port: (CURRENT, None), unit_reader=lambda name: unit,
                                        connection_counter=lambda port: 0, source_dir=temp)
            stale = [board.inspect(spec)['stale_files'] for spec in dashboard.INSTANCES]
        self.assertEqual(stale, [[], ['serve_claude.sh']])

    def test_restart_blocker(self):
        idle = {'state': 'idle', 'active': None, 'queued': 0, 'connections': 0}
        cases = [(idle, None), ({**idle, 'active': {'model': MAX}}, '有推理'), ({**idle, 'queued': 2}, '2 个请求'),
                 ({**idle, 'connections': 3}, '3 个未结束'), ({'state': 'legacy', 'connections': 0}, None),
                 ({'state': 'legacy', 'connections': None}, '无法确认'), ({'state': 'offline', 'connections': None}, None)]
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
                                   ready_wait_s=0.2)

    def test_restart_waits_for_health_and_reports_when_it_does_not_return(self):
        result = self.board((None, 'URLError')).restart('claude')
        self.assertEqual((result['result'], self.calls), ('failed', ['cursor-sdk2api-claude.service']))
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
        healths = {1: (LEGACY, None), 2: (with_active(), None)}
        self.restarts = []
        self.board = dashboard.Dashboard(instances, codex_home=root / 'codex', claude_settings=root / 'missing.json',
                                         claude_state=root / 'state.json', health_reader=lambda port: healths[port],
                                         unit_reader=lambda unit: UNIT, connection_counter=lambda port: 0,
                                         restart_runner=self.restarts.append, source_dir=root, ready_wait_s=0.2)
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

    def test_foreign_host_and_unknown_path_are_refused(self):
        self.assertEqual(self.get('/', 'evil.example:8791')[0], 403)
        self.assertEqual(self.get('/api/status', 'localhost:9999')[0], 200)
        self.assertEqual(self.get('/nope')[0], 404)

    def test_confirm_page_offers_the_tokened_form_only_while_idle(self):
        status, headers, page = self.get('/restart?instance=codex')
        self.assertEqual(status, 200)
        self.assertIn('form-action', headers['Content-Security-Policy'])
        self.assertIn(self.board.token, page)
        self.assertIn('确认重启 cursor-sdk2api.service', page)
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
        self.assertEqual(self.restarts, ['cursor-sdk2api.service'])
        self.assertIn('已重启 cursor-sdk2api.service，/health 已恢复。', self.get('/')[2])
        self.assertEqual(self.post({**good, 'instance': 'claude'})[0], 303)
        self.assertEqual(self.restarts, ['cursor-sdk2api.service'])
        snapshot = json.loads(self.get('/api/status')[2])
        self.assertEqual((snapshot['last_action']['instance'], snapshot['last_action']['result']), ('claude', 'refused'))


if __name__ == '__main__':
    unittest.main()
