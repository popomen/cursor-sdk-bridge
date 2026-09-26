#!/usr/bin/env python3
'''Loopback dashboard for the cursor-sdk2api instances.

Shows service state and request metadata only: no prompts, outputs or credentials. The one action restarts an
adapter unit and is refused while that instance still has a request in flight.
'''
import argparse
from collections import Counter
from datetime import datetime, timedelta
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time
import tomllib
import urllib.parse
import urllib.request

from cursor_bridge import claude_switch, deployment, version

HOME = Path.home()
DEFAULT_PORT = 8791
# Capabilities of the current adapter code; an instance lacking one still runs code from before they existed.
REQUIRED_CAPABILITIES = ('sdk_progress', 'anthropic_messages')
INSTANCES = (
    {'name': 'codex', 'client': 'Codex', 'port': 8789, 'state_dir': HOME / '.codex/cursor-sdk2api',
     'unit': 'cursor-bridge-codex.service', 'limits': {'max': 1200, 'queue_timeout': 1200}},
    {'name': 'claude', 'client': 'Claude Code', 'port': claude_switch.DEFAULT_PORT,
     'state_dir': HOME / '.codex/cursor-sdk2api-claude', 'unit': 'cursor-bridge-claude.service',
     'limits': {'max': claude_switch.MAX_DEADLINE_S, 'queue_timeout': claude_switch.QUEUE_TIMEOUT_S}},
)
LOG_FILES = ('requests.jsonl.3', 'requests.jsonl.2', 'requests.jsonl.1', 'requests.jsonl')
LOG_FIELDS = ('ts', 'event', 'dedup', 'api', 'model', 'stream', 'input_items', 'prompt_bytes', 'images', 'queue_s',
              'outcome', 'duration_s', 'usage')
UNIT_FIELDS = ('ActiveState', 'SubState', 'MainPID', 'NRestarts', 'ActiveEnterTimestamp')
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
STALL_S, RECENT, REFRESH_S, WINDOW_HOURS = 120, 40, 10, 24
RESTART_TIMEOUT_S, READY_WAIT_S = 120, 30
TEXT, HTML = 'text/plain; charset=utf-8', 'text/html; charset=utf-8'
STATES = {'offline': '离线', 'legacy': '旧版代码', 'idle': '空闲', 'running': '推理中', 'stalled': '可能卡住'}
OUTCOMES = {'completed': '完成', 'deadline_expired': '推理超时', 'queue_timeout': '排队超时',
            'request_timeout': '总时限超时', 'client_disconnected': '客户端断开', 'invalid_model_output': '输出格式不合法',
            'upstream_incomplete': '上游未完成', 'model_mismatch': '模型不符', 'isolation_failed': '隔离失败',
            'key_invalid': 'key 无效', 'prompt_too_large': '上下文过长', 'queue_full': '队列已满'}
CSP = ('default-src {0}none{0}; style-src {0}unsafe-inline{0}; form-action {0}self{0}; '
       'frame-ancestors {0}none{0}').format(chr(39))
CSS = ('body{font-family:system-ui,sans-serif;margin:24px;color:#1f2328;background:#f6f8fa}'
       'h1{margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}.muted{color:#656d76;margin:0 0 16px}'
       '.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}'
       '.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px}.card h2{margin:0 0 8px}'
       '.card th{text-align:left;color:#656d76;font-weight:normal;padding:2px 12px 2px 0;white-space:nowrap;'
       'vertical-align:top}.card td{padding:2px 0}table.grid{border-collapse:collapse;background:#fff}'
       '.grid th,.grid td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;font-size:13px;white-space:nowrap}'
       '.grid th{background:#f6f8fa}tr.bad td{background:#fff5f5}.notes{margin:8px 0 0;padding-left:20px;color:#9a6700}'
       'h2 span{font-size:12px;font-weight:normal;padding:2px 8px;border-radius:10px;margin-left:8px}'
       '.state-idle{background:#dafbe1;color:#1a7f37}.state-running{background:#ddf4ff;color:#0969da}'
       '.state-stalled,.state-offline{background:#ffebe9;color:#cf222e}.state-legacy{background:#fff8c5;color:#9a6700}'
       '.button,button{display:inline-block;margin-top:8px;padding:4px 12px;border:1px solid #d0d7de;border-radius:6px;'
       'background:#f6f8fa;color:#1f2328;font-size:13px;text-decoration:none;cursor:pointer}'
       'button.danger{background:#cf222e;border-color:#cf222e;color:#fff}.warn{color:#9a6700;margin:8px 0 0}'
       '.banner{padding:8px 12px;border-radius:6px;margin:0 0 16px}.banner.ok{background:#dafbe1}'
       '.banner.bad{background:#ffebe9}')


def fetch_health(port, timeout=2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:%d/health' % port, timeout=timeout) as response:
            health = json.load(response)
    except Exception as exc:
        return None, type(exc).__name__
    if not isinstance(health, dict) or health.get('service') != 'cursor-sdk2api':
        return None, 'unexpected_service'
    return health, None


def unit_info(unit):
    command = ['systemctl', '--user', 'show', unit, '--no-pager']
    for field in UNIT_FIELDS + ('UnitFileState', 'FragmentPath'):
        command += ['-p', field]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    values = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
    return {field: values.get(field) for field in UNIT_FIELDS + ('UnitFileState', 'FragmentPath')}


def established_connections(port, tables=('/proc/net/tcp', '/proc/net/tcp6')):
    '''Count ESTABLISHED sockets on the local port, or None when no table is readable.

    The adapter speaks HTTP/1.0 and closes each connection after its response, so an open one is a request
    still in flight.
    '''
    total, readable = 0, False
    for table in tables:
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for row in rows:
            fields = row.split()
            if len(fields) > 3 and fields[3] == '01' and fields[1].rsplit(':', 1)[-1] == '%04X' % port:
                total += 1
    return total if readable else None


def run_restart(unit):
    '''Restart one user unit; return None on success or a short failure reason.'''
    try:
        done = subprocess.run(['systemctl', '--user', 'restart', unit], capture_output=True, text=True,
                              timeout=RESTART_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return type(exc).__name__
    return None if done.returncode == 0 else 'systemctl 退出码 %d' % done.returncode


def restart_blocker(item):
    '''Why a restart now would cut a request short, or None when the instance looks idle.'''
    if item.get('active'):
        return '有推理正在进行'
    if item.get('queued'):
        return '%s 个请求在排队' % item['queued']
    if item.get('unfinished'):
        return '%s 个请求或工具仍未完成' % item['unfinished']
    connections = item.get('connections')
    if connections:
        return '端口上有 %d 个未结束的连接，可能有请求正在进行' % connections
    if connections is None and item.get('state') == 'legacy':
        return '旧版服务不上报进度，也读不到连接状态，无法确认是否空闲'
    if not item.get('running_version') or not item.get('deployed_version'):
        return '无法确认运行版本或已部署版本'
    return None


def when(entry):
    try:
        moment = datetime.fromisoformat(entry['ts'])
    except (KeyError, TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.astimezone()


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)] if ordered else None


def seconds(value):
    return '%d 秒' % value if isinstance(value, (int, float)) else '—'


def duration(value):
    if not isinstance(value, (int, float)):
        return '—'
    if value < 60:
        return '%.1f 秒' % value
    minutes, rest = divmod(int(round(value)), 60)
    return '%d 分 %02d 秒' % (minutes, rest)


def count(value):
    if not isinstance(value, (int, float)):
        return '—'
    if value >= 1e6:
        return '%.2fM' % (value / 1e6)
    if value >= 1e4:
        return '%.1fk' % (value / 1e3)
    return '%d' % value


def tier(model):
    return str(model or '?').replace('claude-opus-5-5-', '')


def esc(value):
    return html.escape('—' if value is None else str(value))


def outcome_label(outcome):
    if outcome.startswith('upstream_error:'):
        return '上游异常（%s）' % outcome.split(':', 1)[1]
    return OUTCOMES.get(outcome, outcome)


class LogReader:
    '''Parses request logs, re-reading a file only after its size or mtime changes.'''

    def __init__(self):
        self.cache, self.lock = {}, threading.Lock()

    def entries(self, directory):
        rows = []
        for name in LOG_FILES:
            path = Path(directory) / name
            try:
                info = path.stat()
            except OSError:
                continue
            key = (info.st_mtime_ns, info.st_size)
            with self.lock:
                cached = self.cache.get(path)
            if cached is None or cached[0] != key:
                cached = (key, self.parse(path))
                with self.lock:
                    self.cache[path] = cached
            rows.extend(cached[1])
        return rows

    @staticmethod
    def parse(path):
        rows = []
        try:
            with path.open(encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and isinstance(entry.get('ts'), str):
                        rows.append(entry)
        except OSError:
            pass
        return rows


def summarize(entries, now, hours=WINDOW_HOURS):
    '''Count each inference job once; legacy logs retain their request-based totals.

    HTTP rows describe subscribers/retries and can repeat the inference's usage.
    They have separate access counters and never contribute to model/token totals.
    '''
    since = now - timedelta(hours=hours)
    outcomes, models, tokens, rejected, reported = Counter(), {}, {'input': 0, 'output': 0}, 0, False
    http_outcomes, dedup, inference_records, legacy_records = Counter(), Counter(), 0, 0
    for entry in entries:
        moment = when(entry)
        if moment is None or moment < since:
            continue
        outcome = str(entry.get('outcome', ''))
        event = entry.get('event')
        if event == 'http':
            http_outcomes[outcome] += 1
            if entry.get('dedup') in ('hit', 'joined'):
                dedup[entry['dedup']] += 1
        if outcome.startswith('invalid_request'):
            rejected += 1
            continue
        if event not in (None, 'inference'):
            continue
        if event == 'inference':
            inference_records += 1
        else:
            legacy_records += 1
        outcomes[outcome] += 1
        stats = models.setdefault(str(entry.get('model') or '?'), {'requests': 0, 'completed': 0, 'durations': []})
        stats['requests'] += 1
        if outcome == 'completed':
            stats['completed'] += 1
            if isinstance(entry.get('duration_s'), (int, float)):
                stats['durations'].append(entry['duration_s'])
        usage = entry.get('usage') if isinstance(entry.get('usage'), dict) else {}
        for key, field in (('input', 'input_tokens'), ('output', 'output_tokens'),
                           ('cache_read', 'cache_read_tokens'), ('cache_write', 'cache_write_tokens')):
            if type(usage.get(field)) is int and usage[field] >= 0:
                tokens[key] = tokens.get(key, 0) + usage[field]
                reported = True
    return {'hours': hours, 'requests': sum(outcomes.values()), 'completed': outcomes['completed'],
            'rejected': rejected, 'outcomes': dict(outcomes.most_common()), 'tokens': tokens if reported else None,
            'counting_basis': 'inference_jobs_plus_legacy_requests',
            'inference_records': inference_records, 'legacy_records': legacy_records,
            'http': {'requests': sum(http_outcomes.values()), 'outcomes': dict(http_outcomes), 'dedup': dict(dedup)},
            'models': [{'model': model, 'requests': stats['requests'], 'completed': stats['completed'],
                        'p50_s': percentile(stats['durations'], 0.5), 'p90_s': percentile(stats['durations'], 0.9),
                        'max_s': max(stats['durations'], default=None)} for model, stats in sorted(models.items())]}


def describe(spec, health, error):
    report = {'name': spec['name'], 'client': spec['client'], 'port': spec['port'], 'unit_name': spec['unit'],
              'expected_limits': spec['limits'], 'notes': []}
    notes = report['notes']
    if health is None:
        notes.append('127.0.0.1:%s 没有响应（%s），检查 %s。' % (spec['port'], error, spec['unit']))
        return {**report, 'state': 'offline', 'error': error}
    progress = health.get('progress') if isinstance(health.get('progress'), dict) else None
    limits = health.get('limits') if isinstance(health.get('limits'), dict) else None
    active = progress.get('active') if progress and isinstance(progress.get('active'), dict) else None
    report.update(adapter_version=health.get('adapter_version'), limits=limits, active=active,
                  queued=progress.get('queued') if progress else None,
                  unfinished=progress.get('unfinished', health.get('unfinished')) if progress else None,
                  running_version=health.get('running_version'))
    missing = [name for name in REQUIRED_CAPABILITIES if name not in (health.get('capabilities') or [])]
    if missing or limits is None:
        notes.append('仍在运行旧版适配器代码（缺少 %s），没有进行中的请求时重启 %s 才会生效。'
                     % ('、'.join(missing + ([] if limits else ['limits'])), spec['unit']))
    else:
        deadlines = limits.get('deadlines') if isinstance(limits.get('deadlines'), dict) else {}
        expected = spec['limits']
        if deadlines.get('max') != expected['max'] or limits.get('queue_timeout') != expected['queue_timeout']:
            notes.append('max 推理时限 %s、排队上限 %s，预期分别为 %s、%s；实例空闲后用 cursor-bridge restart %s 重启。' % (
                seconds(deadlines.get('max')), seconds(limits.get('queue_timeout')), seconds(expected['max']),
                seconds(expected['queue_timeout']), spec['name']))
    if progress is None:
        state = 'legacy'
    elif active is None:
        state = 'idle'
    else:
        idle, running, deadline = active.get('idle_s') or 0, active.get('running_s') or 0, active.get('deadline_s')
        state = 'stalled' if idle >= STALL_S else 'running'
        if state == 'stalled':
            notes.append('已经 %s 没有新的 SDK 事件，上游可能卡住；到时限会报 deadline_expired。' % duration(idle))
        if deadline and running >= 0.8 * deadline:
            notes.append('已运行 %s，接近 %s 的推理时限。' % (duration(running), duration(deadline)))
    if report['queued']:
        notes.append('%s 个请求在排队，同一实例的其他会话要等当前推理结束。' % report['queued'])
    return {**report, 'state': state}


class Dashboard:
    def __init__(self, instances=INSTANCES, codex_home=None, claude_settings=None, claude_state=None,
                 health_reader=fetch_health, unit_reader=unit_info, connection_counter=established_connections,
                 restart_runner=run_restart, ready_wait_s=READY_WAIT_S,
                 admin_client=deployment.admin_request, version_reader=version.deployed_version):
        codex = Path(codex_home or os.environ.get('CODEX_HOME') or HOME / '.codex')
        claude_dir = Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude')
        self.instances, self.codex_home = instances, codex
        self.claude_settings = Path(claude_settings or claude_dir / 'settings.json')
        self.claude_state = Path(claude_state or codex / 'cursor-fallback-state/claude-code.json')
        self.health_reader, self.unit_reader, self.logs = health_reader, unit_reader, LogReader()
        self.connection_counter, self.restart_runner = connection_counter, restart_runner
        self.ready_wait_s = ready_wait_s
        self.admin_client, self.version_reader = admin_client, version_reader
        # Proves a restart form came from this process's own page; other sites cannot read it.
        self.token = secrets.token_urlsafe(32)
        self.restart_lock, self.last_action = threading.Lock(), None

    def spec(self, name):
        return next((spec for spec in self.instances if spec['name'] == name), None)

    def inspect(self, spec):
        health, error = self.health_reader(spec['port'])
        unit = self.unit_reader(spec['unit'])
        item = describe(spec, health, error)
        deployed = self.version_reader()
        running = item.get('running_version')
        item.update(unit=unit, connections=self.connection_counter(spec['port']),
                    deployed_version=deployed, needs_restart=bool(deployed and deployed != running))
        if item['needs_restart'] and item['state'] != 'offline':
            item['notes'].append('运行版本 %s 与已部署版本 %s 不同，实例空闲后重启。' % (running or '未知', deployed))
        item['restart_blocker'] = restart_blocker(item)
        return item

    def clients(self):
        codex = {'client': 'Codex'}
        try:
            config = tomllib.loads((self.codex_home / 'config.toml').read_text(encoding='utf-8'))
            codex.update(provider=config.get('model_provider', 'openai'), model=config.get('model'),
                         effort=config.get('model_reasoning_effort'))
        except Exception as exc:
            codex['error'] = type(exc).__name__
        claude = {'client': 'Claude Code'}
        try:
            report = claude_switch.status(self.claude_settings, self.claude_state)
            claude.update(provider=report['provider'], model=report['model'], base_url=report['base_url'])
        except Exception as exc:
            claude['error'] = type(exc).__name__
        return [codex, claude]

    def snapshot(self):
        now = datetime.now().astimezone()
        instances, recent = [], []
        for spec in self.instances:
            entries = self.logs.entries(Path(spec['state_dir']) / 'logs')
            instances.append({**self.inspect(spec), 'summary': summarize(entries, now)})
            for entry in entries:
                if when(entry) and not str(entry.get('outcome', '')).startswith('invalid_request'):
                    recent.append({'instance': spec['name'], **{key: entry[key] for key in LOG_FIELDS if key in entry}})
        recent.sort(key=when, reverse=True)
        return {'generated_at': now.isoformat(timespec='seconds'), 'refresh_s': REFRESH_S, 'instances': instances,
                'clients': self.clients(), 'recent': recent[:RECENT], 'last_action': self.last_action, 'running_version': version.running_version(),
                'deployed_version': self.version_reader()}

    def restart(self, name):
        """Close admission atomically, then require zero work and connections before restarting."""
        spec = self.spec(name)
        if spec is None:
            return None
        if not self.restart_lock.acquire(blocking=False):
            return self.record(spec, 'refused', '另一个重启还没结束，稍后再试。')
        drained, restarted = False, False
        try:
            item = self.inspect(spec)
            blocker = item['restart_blocker']
            if blocker:
                return self.record(spec, 'refused', '%s，没有重启 %s。等请求结束后再试。' % (blocker, spec['unit']))
            expected_version = item['deployed_version']
            try:
                # Mark before sending: a lost response may still have closed admission.
                drained = True
                state = self.admin_client(spec['port'], 'drain')
                if (state.get('draining') is not True or type(state.get('unfinished')) is not int
                        or state['unfinished'] != 0):
                    return self.record(spec, 'refused', '实例仍有未完成请求或无法确认已停止接收新请求。')
                # admin_request has consumed and closed its HTTP connection before this check.
                connections = self.connection_counter(spec['port'])
                if connections is None or connections != 0:
                    return self.record(spec, 'refused', '端口仍有未结束连接或连接状态未知，没有重启。')
            except Exception as exc:
                return self.record(spec, 'refused', '无法安全暂停接收新请求（%s），没有重启。' % type(exc).__name__)
            error = self.restart_runner(spec['unit'])
            if error:
                return self.record(spec, 'failed', '重启 %s 失败（%s）。' % (spec['unit'], error))
            deadline = time.monotonic() + self.ready_wait_s
            while True:
                health = self.health_reader(spec['port'])[0]
                if (health is not None and not health.get('draining', False)
                        and health.get('running_version') == expected_version):
                    break
                if time.monotonic() >= deadline:
                    return self.record(spec, 'failed', '已重启 %s，但 %g 秒内预期版本的 /health 没有恢复，用 systemctl --user '
                                       'status %s 检查。' % (spec['unit'], self.ready_wait_s, spec['unit']))
                time.sleep(0.2)
            restarted = True
            return self.record(spec, 'restarted', '已重启 %s，/health 已恢复。' % spec['unit'])
        finally:
            if drained and not restarted:
                try:
                    self.admin_client(spec['port'], 'resume')
                except Exception:
                    if self.last_action:
                        self.last_action['message'] += ' 自动恢复接收请求失败；检查 /health 的 draining 状态。'
            self.restart_lock.release()

    def record(self, spec, result, message):
        self.last_action = {'at': datetime.now().astimezone().isoformat(timespec='seconds'), 'instance': spec['name'],
                            'unit': spec['unit'], 'result': result, 'message': message}
        return self.last_action


def card(item):
    unit, limits, active = item.get('unit'), item.get('limits'), item.get('active')
    unit_text = None
    if unit:
        unit_text = '%s/%s，PID %s，自动重启 %s 次，启动于 %s' % tuple(unit.get(field) or '—' for field in UNIT_FIELDS)
    limit_text = '未上报'
    if limits:
        deadlines = limits.get('deadlines') or {}
        limit_text = 'high %s / xhigh %s / max %s，排队上限 %s' % tuple(
            seconds(value) for value in (deadlines.get('high'), deadlines.get('xhigh'), deadlines.get('max'),
                                          limits.get('queue_timeout')))
    active_text = '未知' if item['state'] in ('offline', 'legacy') else '无'
    if active:
        active_text = '%s：已运行 %s / 时限 %s，SDK 事件 %s，距上次事件 %s' % (
            tier(active.get('model')), duration(active.get('running_s')), duration(active.get('deadline_s')),
            active.get('events'), duration(active.get('idle_s')))
    rows = (('客户端', item['client']), ('地址', '127.0.0.1:%s' % item['port']),
            ('适配器版本', item.get('adapter_version')), ('运行提交', item.get('running_version')),
            ('已部署提交', item.get('deployed_version')), ('需要重启', item.get('needs_restart')),
            ('systemd', unit_text), ('时限', limit_text),
            ('当前推理', active_text), ('排队', item.get('queued')), ('未结束连接', item.get('connections')))
    table = ''.join('<tr><th>%s</th><td>%s</td></tr>' % (esc(name), esc(value)) for name, value in rows)
    notes = ''.join('<li>%s</li>' % esc(note) for note in item['notes'])
    if item.get('restart_blocker'):
        action = '<p class=warn>暂不能重启：%s</p>' % esc(item['restart_blocker'])
    else:
        action = '''<a class=button href='/restart?instance=%s'>重启 %s…</a>''' % (esc(item['name']), esc(item['unit_name']))
    return '<article class=card><h2>%s<span class=state-%s>%s</span></h2><table>%s</table>%s%s</article>' % (
        esc(item['name']), item['state'], esc(STATES[item['state']]), table,
        '<ul class=notes>%s</ul>' % notes if notes else '', action)


def clients_table(clients):
    rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        esc(client['client']), esc(client.get('provider')), esc(client.get('model')),
        esc('读取失败（%s）' % client['error'] if client.get('error') else client.get('base_url') or client.get('effort')))
        for client in clients)
    return ('<h2>客户端配置</h2><table class=grid><tr><th>客户端</th><th>provider</th><th>默认模型</th>'
            '<th>备注</th></tr>%s</table>' % rows)


def summary_block(item):
    summary = item['summary']
    text = '推理任务 %d' % summary['requests']
    if summary['legacy_records']:
        text += '（含旧版请求记录 %d）' % summary['legacy_records']
    text += ' · HTTP 访问 %d' % summary['http']['requests']
    outcomes = ' · '.join('%s %d' % (outcome_label(name), total) for name, total in summary['outcomes'].items())
    if outcomes:
        text += ' · ' + outcomes
    if summary['rejected']:
        text += '；预检拒绝 %d' % summary['rejected']
    if summary['tokens']:
        text += '；SDK token 输入 %s、输出 %s' % (count(summary['tokens']['input']), count(summary['tokens']['output']))
        for field, label in (('cache_read', '缓存读取'), ('cache_write', '缓存写入')):
            if field in summary['tokens']:
                text += '、%s %s' % (label, count(summary['tokens'][field]))
    rows = ''.join('<tr><td>%s</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        esc(tier(model['model'])), model['requests'], model['completed'], esc(duration(model['p50_s'])),
        esc(duration(model['p90_s'])), esc(duration(model['max_s']))) for model in summary['models'])
    table = ('<table class=grid><tr><th>档位</th><th>推理任务</th><th>完成</th><th>完成耗时 p50</th><th>p90</th>'
             '<th>最长</th></tr>%s</table>' % rows) if rows else ''
    return '<h2>%s（%s）· 最近 %d 小时</h2><p>%s</p>%s' % (
        esc(item['client']), esc(item['name']), summary['hours'], esc(text), table)


def recent_table(recent):
    header = ''.join('<th>%s</th>' % name for name in ('时间', '实例', '记录', '接口', '档位', '结果', '耗时', '排队', 'prompt',
                                                        '输入 token', '输出 token'))
    rows = []
    for entry in recent:
        event = entry.get('event')
        usage = entry.get('usage') if event in (None, 'inference') and isinstance(entry.get('usage'), dict) else {}
        label = {None: '旧版请求', 'inference': '推理', 'http': 'HTTP'}.get(event, '其他')
        dedup = {'hit': '缓存命中', 'joined': '共享推理'}.get(entry.get('dedup'))
        if dedup:
            label += '（%s）' % dedup
        outcome, prompt = str(entry.get('outcome', '')), entry.get('prompt_bytes')
        cells = (str(entry.get('ts', ''))[5:19].replace('T', ' '), entry['instance'], label, entry.get('api') or 'responses',
                 tier(entry.get('model')), outcome_label(outcome), duration(entry.get('duration_s')),
                 duration(entry.get('queue_s')), '%d KB' % (prompt // 1024) if isinstance(prompt, int) else None,
                 count(usage.get('input_tokens')), count(usage.get('output_tokens')))
        rows.append('<tr class=%s>%s</tr>' % ('ok' if outcome == 'completed' else 'bad',
                                              ''.join('<td>%s</td>' % esc(cell) for cell in cells)))
    body = ''.join(rows) or '<tr><td colspan=11>暂无记录</td></tr>'
    return ('<h2>最近事件</h2><p class=muted>HTTP 访问与重试不重复计入推理或 token 总量。</p>'
            '<table class=grid><tr>%s</tr>%s</table>') % (header, body)


def page(body, refresh=None):
    meta = '<meta http-equiv=refresh content=%d>' % refresh if refresh else ''
    return ('<!doctype html><html lang=zh-CN><head><meta charset=utf-8>' + meta
            + '''<meta name=viewport content='width=device-width, initial-scale=1'>'''
            + '<title>cursor-sdk2api</title><style>' + CSS + '</style></head><body>' + body + '</body></html>')


def render(snapshot):
    action, banner = snapshot.get('last_action'), ''
    if action:
        banner = '''<p class='banner %s'>%s %s</p>''' % ('ok' if action['result'] == 'restarted' else 'bad',
                                                        esc(action['at'][11:19]), esc(action['message']))
    intro = ('<h1>cursor-sdk2api</h1><p class=muted>更新于 %s，每 %d 秒自动刷新。只显示元数据，不含提示词、输出或凭据；'
             '唯一的操作是在实例空闲时重启它。</p>' % (esc(snapshot['generated_at']), snapshot['refresh_s']))
    cards = '<section class=cards>%s</section>' % ''.join(card(item) for item in snapshot['instances'])
    summaries = ''.join(summary_block(item) for item in snapshot['instances'])
    return page(intro + banner + cards + clients_table(snapshot['clients']) + summaries
                + recent_table(snapshot['recent']), snapshot['refresh_s'])


def render_confirm(item, token):
    blocker = item['restart_blocker']
    if blocker:
        action = ('<p class=warn>%s，现在不能重启。等请求结束后刷新本页再确认；确需中断时在终端执行 '
                  'systemctl --user restart %s。</p>' % (esc(blocker), esc(item['unit_name'])))
    else:
        action = ('''<form method=post action=/restart><input type=hidden name=token value='%s'>'''
                  '''<input type=hidden name=instance value='%s'>'''
                  '''<button type=submit class=danger>确认重启 %s</button></form>''') % (
                      esc(token), esc(item['name']), esc(item['unit_name']))
    active = '有' if item.get('active') else ('未知' if item['state'] in ('offline', 'legacy') else '无')
    rows = (('状态', STATES[item['state']]), ('当前推理', active), ('排队', item.get('queued')),
            ('未结束连接', item.get('connections')))
    table = ''.join('<tr><th>%s</th><td>%s</td></tr>' % (esc(name), esc(value)) for name, value in rows)
    notes = ''.join('<li>%s</li>' % esc(note) for note in item['notes'])
    body = ('<h1>重启 %s</h1><p class=muted>将执行 systemctl --user restart %s。重启会结束这个实例上正在进行的请求，'
            '所以有请求时会拒绝；服务通常几秒内恢复。只重启适配服务，不需要断开 Desktop，也不用重启 Claude Code。</p>'
            '<article class=card><table>%s</table>%s%s</article><p><a class=button href=/>返回</a></p>') % (
                esc(item['unit_name']), esc(item['unit_name']), table,
                '<ul class=notes>%s</ul>' % notes if notes else '', action)
    return page(body)


def local_host(value):
    host = (value or '').strip().lower()
    if host.startswith('['):
        host = host[:host.find(']') + 1]
    elif ':' in host:
        host = host.rsplit(':', 1)[0]
    return host in LOCAL_HOSTS


def same_site(headers):
    '''Refuse browser posts from other sites; the per-process token is the main defence.'''
    site = headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        return False
    origin = headers.get('Origin')
    return origin is None or urllib.parse.urlsplit(origin).hostname in ('127.0.0.1', 'localhost', '::1')


class Handler(BaseHTTPRequestHandler):
    server_version = 'cursor-sdk2api-dashboard'

    def log_message(self, format, *args):
        pass

    def send(self, status, body, content_type=TEXT, headers=()):
        data = body.encode()
        self.send_response(status)
        for name, value in (('Content-Type', content_type), ('Content-Length', str(len(data))),
                            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                            ('Content-Security-Policy', CSP)) + tuple(headers):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def respond(self, parts):
        board = self.server.dashboard
        if parts.path == '/restart':
            spec = board.spec(urllib.parse.parse_qs(parts.query).get('instance', [''])[0])
            if spec is None:
                return 404, 'unknown instance', TEXT
            return 200, render_confirm(board.inspect(spec), board.token), HTML
        if parts.path == '/health':
            return 200, json.dumps({'service': 'cursor-bridge-dashboard', 'status': 'ready',
                                    'running_version': version.running_version(),
                                    'deployed_version': board.version_reader()}), 'application/json'
        if parts.path == '/api/status':
            return 200, json.dumps(board.snapshot(), ensure_ascii=False), 'application/json'
        if parts.path == '/':
            return 200, render(board.snapshot()), HTML
        return 404, 'not found', TEXT

    def do_GET(self):
        # Loopback only; refusing other Host names also blocks DNS rebinding from a browser tab.
        if not local_host(self.headers.get('Host')):
            return self.send(403, 'forbidden host')
        try:
            status, body, content_type = self.respond(urllib.parse.urlsplit(self.path))
        except Exception as exc:
            status, body, content_type = 500, 'dashboard failed: ' + type(exc).__name__, TEXT
        self.send(status, body, content_type)

    def do_POST(self):
        if not local_host(self.headers.get('Host')) or not same_site(self.headers):
            return self.send(403, 'forbidden')
        if self.path.split('?', 1)[0] != '/restart':
            return self.send(404, 'not found')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        form = (self.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
        if form != 'application/x-www-form-urlencoded' or not 0 < length <= 4096:
            return self.send(400, 'bad request')
        try:
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'), max_num_fields=8)
        except ValueError:
            return self.send(400, 'bad request')
        board = self.server.dashboard
        token = fields.get('token', [''])[0]
        if not hmac.compare_digest(token.encode(), board.token.encode()):
            return self.send(403, 'missing or stale token; reload the dashboard and try again')
        if board.restart(fields.get('instance', [''])[0]) is None:
            return self.send(404, 'unknown instance')
        self.send(303, '', TEXT, (('Location', '/'),))


def make_server(board, port=DEFAULT_PORT):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.dashboard = board
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = make_server(Dashboard(), args.port)

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print('cursor-sdk2api dashboard on http://127.0.0.1:%d/' % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
