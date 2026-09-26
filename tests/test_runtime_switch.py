"""Isolated regression tests: no real daemon, credentials, or network access.

Run with unittest discovery from the owning skill's scripts directory.
"""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from cursor_sdk_bridge import appserver_runtime as ar
from cursor_sdk_bridge import runtime_switch as rs
from cursor_sdk_bridge import switch_config as sc


class FakeRuntime:
    """Stateful daemon fixture honoring the production ownership contract."""
    matches = staticmethod(ar.DesktopRuntime.matches)

    def __init__(self, directory):
        self.directory = directory
        self.next_pid = 100
        self.live = self.snapshot()
        self.stopped = []
        self.started = []
        self.actions = []
        self.blocked = None

    def snapshot(self, *, models=None, pid=None, modalities=None):
        config = tomllib.loads((self.directory / 'config.toml').read_text())
        selected = {key: config.get(key) for key in ar.CONFIG_KEYS}
        selected['model_provider'] = selected['model_provider'] or 'openai'
        if models is None:
            models = sorted(ar.ALIASES) if selected['model_provider'] == 'cursor' else [selected['model'] or 'gpt-6-astra']
        if modalities is None and selected['model_provider'] == 'cursor':
            modalities = ar.catalog_modalities(selected['model_catalog_json'])
        return {'state': 'running', 'pid': pid or self.next_pid,
                'start_ticks': 10 * (pid or self.next_pid), 'executable': '/fixture/codex',
                'config': selected, 'models': models, 'default_models': models[:1], 'modalities': modalities or {},
                'active_tasks': 0}

    def inspect(self, tasks=False):
        return copy.deepcopy(self.live)

    def idle(self, expected=None):
        if self.blocked:
            raise ar.RuntimeBlocked(self.blocked)
        if self.live.get('active_tasks'):
            raise ar.RuntimeBlocked('App-server has active tasks.')
        if expected and self.live['state'] == 'running' and not rs.same_process(self.live, expected):
            raise ar.RuntimeBlocked('Unexpected daemon identity.')
        return self.inspect()

    def stop(self, expected):
        self.idle(expected)
        if self.live['state'] == 'running':
            self.stopped.append(copy.deepcopy(expected))
            self.live = {'state': 'absent'}

    def start(self, executable=None):
        if self.live['state'] == 'running':
            raise ar.RuntimeBlocked('An unexpected daemon appeared; it was not adopted or stopped.')
        self.next_pid += 1
        action = self.actions.pop(0) if self.actions else 'ok'
        self.started.append(action)
        if action == 'fail':
            raise ar.RuntimeBlocked('Fixture daemon startup failed.')
        if action == 'external':
            self.live = self.snapshot(models=['unrelated-model'], pid=999)
            raise ar.RuntimeBlocked('A different daemon won the startup race; it was not stopped.')
        if action == 'mismatch':
            self.live = self.snapshot(models=['wrong-model'])
        elif callable(action):
            self.live = self.snapshot()
            action()
        else:
            self.live = self.snapshot()
        return self.inspect()


class RuntimeSwitchIncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cursor-runtime-incident-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.original = b'# fixture configuration\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "ultra"\n'
        (self.directory / 'config.toml').write_bytes(self.original)
        self.auth = b'{"auth_mode":"fixture-only","fixture":"not-a-real-credential"}'
        (self.directory / 'auth.json').write_bytes(self.auth)
        self.runtime = FakeRuntime(self.directory)
        for name in ('cursor_sdk_bridge.runtime_switch.verify_service', 'cursor_sdk_bridge.switch_config.verify_service', 'cursor_sdk_bridge.runtime_switch.probe'):
            p = patch(name)
            p.start()
            self.addCleanup(p.stop)
        for name in ('urllib.request.OpenerDirector.open', 'socket.create_connection'):
            guard = patch(name, side_effect=AssertionError('Network forbidden in isolated regression test'))
            guard.start()
            self.addCleanup(guard.stop)

    @property
    def journal(self):
        return self.directory / 'cursor-fallback-state/runtime-transition.json'

    def apply(self, mode, restart=True):
        return rs.apply_mode(self.directory, mode, restart=restart, runtime=self.runtime)

    def assert_auth_preserved(self):
        self.assertEqual((self.directory / 'auth.json').read_bytes(), self.auth)

    def install_old_catalog(self):
        old_root = self.directory / 'old-skill'
        (old_root / 'assets').mkdir(parents=True)
        (old_root / 'assets/models.json').write_bytes((sc.ROOT / 'assets/models.json').read_bytes())
        with patch.object(sc, 'ROOT', old_root):
            self.apply('cursor')
        return old_root / 'assets/models.json'

    def old_cursor_files(self):
        return ((self.directory / 'config.toml').read_bytes(),
                (self.directory / 'cursor-fallback-state/state.json').read_bytes())

    def test_catalog_path_upgrade_needs_explicit_restart_even_when_runtime_matches(self):
        self.install_old_catalog()
        before = self.old_cursor_files()
        config = tomllib.loads(before[0].decode())
        self.assertTrue(self.runtime.matches(self.runtime.live, config))
        stopped = len(self.runtime.stopped)
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'older installation'):
            self.apply('cursor', restart=False)
        self.assertEqual(len(self.runtime.stopped), stopped)
        self.assertEqual(self.old_cursor_files(), before)
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_catalog_path_upgrade_restarts_and_verifies_current_catalog(self):
        self.install_old_catalog()
        before_state = json.loads(self.old_cursor_files()[1])
        stopped = len(self.runtime.stopped)
        observed = self.apply('cursor')
        self.assertEqual(len(self.runtime.stopped), stopped + 1)
        self.assertEqual(observed['config']['model_catalog_json'], str(sc.ROOT / 'assets/models.json'))
        self.assertEqual(json.loads(self.old_cursor_files()[1])['original_config'], before_state['original_config'])
        self.assertTrue(self.runtime.matches(observed, tomllib.loads(self.old_cursor_files()[0].decode())))
        self.assertFalse(sc.status(self.directory)['catalog_path_upgrade_required'])
        self.assert_auth_preserved()

    def test_failed_catalog_upgrade_rolls_back_exact_config_and_state_bytes(self):
        old = self.install_old_catalog()
        before = self.old_cursor_files()
        self.runtime.actions = ['fail', 'ok']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'Previous mode was restored'):
            self.apply('cursor')
        self.assertEqual(self.old_cursor_files(), before)
        self.assertEqual(self.runtime.live['config']['model_catalog_json'], str(old))
        self.assertTrue(self.runtime.matches(self.runtime.live, tomllib.loads(before[0].decode())))
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_catalog_upgrade_preserves_external_state_and_retains_recovery_journal(self):
        self.install_old_catalog()
        state_path = self.directory / 'cursor-fallback-state/state.json'
        real_switch = sc.switch
        def external_state(*args, **kwargs):
            real_switch(*args, **kwargs)
            state_path.write_bytes(state_path.read_bytes() + b'\n')
        started = len(self.runtime.started)
        with patch.object(rs, 'switch', side_effect=external_state):
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
                self.apply('cursor')
        self.assertTrue(state_path.read_bytes().endswith(b'\n'))
        self.assertEqual(len(self.runtime.started), started)
        self.assertTrue(self.journal.exists())
        self.assert_auth_preserved()
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'External file changes'):
            self.apply('cursor')
        self.assertTrue(state_path.read_bytes().endswith(b'\n'))

    def test_catalog_upgrade_after_double_start_failure_does_not_report_old_path_as_success(self):
        self.install_old_catalog()
        before = self.old_cursor_files()
        self.runtime.actions = ['fail', 'fail']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
            self.apply('cursor')
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'Run the desired switch again'):
            self.apply('cursor')
        self.assertEqual(self.old_cursor_files(), before)
        self.assertTrue(sc.status(self.directory)['catalog_path_upgrade_required'])
        self.assertFalse(self.journal.exists())
        self.apply('cursor')
        self.assertFalse(sc.status(self.directory)['catalog_path_upgrade_required'])
        self.assert_auth_preserved()

    def test_catalog_upgrade_missing_backup_refuses_before_daemon_stop(self):
        self.install_old_catalog()
        (self.directory / 'cursor-fallback-state/state.json').unlink()
        stopped = len(self.runtime.stopped)
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'No saved OpenAI baseline'):
            self.apply('cursor')
        self.assertEqual(len(self.runtime.stopped), stopped)
        self.assertFalse(self.journal.exists())

    def test_bidirectional_switch_verifies_consumer_and_preserves_auth(self):
        first_pid = self.runtime.live['pid']
        cursor = self.apply('cursor')
        self.assertNotEqual(cursor['pid'], first_pid)
        self.assertEqual(cursor['config']['model_provider'], 'cursor')
        self.assertEqual(set(cursor['models']), ar.ALIASES)
        self.assertFalse(self.journal.exists())
        self.assertTrue((self.directory / 'cursor-fallback-state/state.json').exists())
        self.assert_auth_preserved()
        cursor_provider = tomllib.loads((self.directory / 'config.toml').read_text())['model_providers']['cursor']
        restored = self.apply('openai')
        self.assertNotEqual(restored['pid'], cursor['pid'])
        self.assertEqual(restored['config']['model_provider'], 'openai')
        restored_config = tomllib.loads((self.directory / 'config.toml').read_text())
        self.assertEqual(restored_config.pop('model_providers'), {'cursor': cursor_provider})
        self.assertEqual(restored_config, tomllib.loads(self.original.decode()))
        self.assertTrue((self.directory / 'config.toml').read_bytes().startswith(b'# fixture configuration\n'))
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.directory / 'cursor-fallback-state/state.json').exists())
        self.assert_auth_preserved()
        audit = json.loads((self.directory / 'cursor-fallback-state/last-switch.json').read_text())
        self.assertEqual(audit['result'], 'runtime_verified')
        self.assertEqual(audit['pid'], restored['pid'])

    def test_failed_restore_rolls_back_cursor_registry_and_state_exactly(self):
        self.apply('cursor')
        before = self.old_cursor_files()
        self.runtime.actions = ['fail', 'ok']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'Previous mode was restored'):
            self.apply('openai')
        self.assertEqual(self.old_cursor_files(), before)
        self.assertEqual(self.runtime.live['config']['model_provider'], 'cursor')
        self.assertFalse(self.journal.exists())
        restored = self.apply('openai')
        self.assertEqual(restored['config']['model_provider'], 'openai')
        config = tomllib.loads((self.directory / 'config.toml').read_text())
        self.assertEqual(config['model_providers']['cursor'],
                         tomllib.loads(before[0].decode())['model_providers']['cursor'])
        self.assert_auth_preserved()

    def test_requires_explicit_restart_before_any_mutation(self):
        with self.assertRaises(ar.RuntimeBlocked):
            self.apply('cursor', restart=False)
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_service_preflight_failure_does_not_stop_healthy_daemon(self):
        with patch('cursor_sdk_bridge.runtime_switch.verify_service', side_effect=ValueError('fixture service down')):
            with self.assertRaises(ValueError):
                self.apply('cursor')
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_live_sdk_failure_leaves_openai_runtime_and_files_untouched(self):
        with patch('cursor_sdk_bridge.runtime_switch.probe', side_effect=RuntimeError('fixture SDK failure')):
            with self.assertRaises(RuntimeError):
                self.apply('cursor')
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_missing_daemon_does_not_launch_a_path_wrapper(self):
        self.runtime.live = {'state': 'absent'}
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'No running daemon'):
            self.apply('cursor')
        self.assertEqual(self.runtime.started, [])
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)

    def test_active_tasks_reject_before_any_mutation(self):
        self.runtime.live['active_tasks'] = 1
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'active tasks'):
            self.apply('cursor')
        self.assertEqual(self.runtime.stopped, [])
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assertFalse(self.journal.exists())

    def test_external_edit_after_file_switch_is_preserved(self):
        real_switch = sc.switch
        def switch_with_external_editor(*args, **kwargs):
            real_switch(*args, **kwargs)
            target = self.directory / 'config.toml'
            target.write_bytes(b'# concurrent external editor\n' + target.read_bytes())
        with patch('cursor_sdk_bridge.runtime_switch.switch', side_effect=switch_with_external_editor):
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
                self.apply('cursor')
        self.assertIn(b'# concurrent external editor\n', (self.directory / 'config.toml').read_bytes())
        self.assertEqual(self.runtime.started, [])
        self.assertTrue(self.journal.exists())
        self.assert_auth_preserved()
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'External file changes'):
            self.apply('openai')
        self.assertIn(b'# concurrent external editor\n', (self.directory / 'config.toml').read_bytes())

    def test_external_edit_during_runtime_validation_is_preserved(self):
        def edit():
            target = self.directory / 'config.toml'
            target.write_bytes(b'# changed while starting\n' + target.read_bytes())
        self.runtime.actions = [edit]
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
            self.apply('cursor')
        self.assertIn(b'# changed while starting\n', (self.directory / 'config.toml').read_bytes())
        # Only the original idle daemon was stopped; conflict checking must
        # happen before stopping a newly running consumer.
        self.assertEqual(len(self.runtime.stopped), 1)
        self.assertEqual(self.runtime.live['state'], 'running')
        self.assert_auth_preserved()

    def test_external_edit_while_rollback_inspects_runtime_is_preserved(self):
        self.runtime.actions = ['fail']
        original_idle = self.runtime.idle
        changed = False
        def idle_with_external_editor(*args, **kwargs):
            nonlocal changed
            if self.runtime.started and not changed:
                changed = True
                target = self.directory / 'config.toml'
                target.write_bytes(b'# edited during rollback inspection\n' + target.read_bytes())
            return original_idle(*args, **kwargs)
        with patch.object(self.runtime, 'idle', side_effect=idle_with_external_editor):
            with self.assertRaises(ar.RuntimeBlocked):
                self.apply('cursor')
        self.assertTrue(changed)
        self.assertIn(b'# edited during rollback inspection\n', (self.directory / 'config.toml').read_bytes())
        self.assertTrue(self.journal.exists())
        self.assert_auth_preserved()

    def test_two_startup_failures_can_later_recover_with_openai(self):
        self.runtime.actions = ['fail', 'fail']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
            self.apply('cursor')
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assertTrue(self.journal.exists())
        recovered = self.apply('openai')
        self.assertEqual(recovered['config']['model_provider'], 'openai')
        self.assertFalse(self.journal.exists())
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        self.assert_auth_preserved()

    def test_wrong_catalog_then_rollback_start_failure_is_recoverable(self):
        self.runtime.actions = ['mismatch', 'fail']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
            self.apply('cursor')
        self.assertEqual((self.directory / 'config.toml').read_bytes(), self.original)
        recovered = self.apply('openai')
        self.assertEqual(recovered['config']['model_provider'], 'openai')
        self.assertFalse(self.journal.exists())
        self.assert_auth_preserved()

    def test_external_daemon_is_not_owned_or_stopped_on_failed_start(self):
        self.runtime.actions = ['external']
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'recovery journal retained'):
            self.apply('cursor')
        self.assertEqual(self.runtime.live['pid'], 999)
        self.assertEqual([p['pid'] for p in self.runtime.stopped], [100])
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal['owned'], [])
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'external daemon'):
            self.apply('openai')
        self.assertEqual(self.runtime.live['pid'], 999)
        self.assert_auth_preserved()

    def test_same_mode_cursor_still_checks_adapter_health(self):
        self.apply('cursor')
        count = len(self.runtime.stopped)
        with patch('cursor_sdk_bridge.runtime_switch.verify_service', side_effect=ValueError('fixture old adapter')):
            with self.assertRaises(ValueError):
                self.apply('cursor', restart=False)
        self.assertEqual(len(self.runtime.stopped), count)
        self.assert_auth_preserved()

    def test_stale_daemon_catalog_needs_restart_and_restart_reloads_it(self):
        self.apply('cursor')
        config = tomllib.loads((self.directory / 'config.toml').read_text())
        self.assertTrue(self.runtime.matches(self.runtime.live, config))
        self.runtime.live = self.runtime.snapshot(pid=self.runtime.live['pid'],
                                                  modalities={slug: ['text'] for slug in ar.ALIASES})
        self.assertFalse(self.runtime.matches(self.runtime.live, config))
        self.assertTrue(ar.catalog_reload_required(self.runtime.live, config))
        stopped = len(self.runtime.stopped)
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'model catalog it loaded'):
            self.apply('cursor', restart=False)
        self.assertEqual(len(self.runtime.stopped), stopped)
        reloaded = self.apply('cursor')
        self.assertEqual(len(self.runtime.stopped), stopped + 1)
        self.assertTrue(self.runtime.matches(reloaded, config))
        self.assertFalse(ar.catalog_reload_required(reloaded, config))
        self.assert_auth_preserved()

    def test_catalog_stale_compares_every_catalog_model(self):
        catalog = str(Path(ar.__file__).resolve().parent / 'assets/models.json')
        expected = ar.catalog_modalities(catalog)
        self.assertEqual(expected, {slug: ['image', 'text'] for slug in ar.ALIASES})
        config = {'model_catalog_json': catalog}
        self.assertFalse(ar.DesktopRuntime.catalog_stale({'modalities': expected}, config))
        for loaded in ({}, None, {**expected, 'claude-opus-5-5-max': ['text']}):
            with self.subTest(loaded=loaded):
                self.assertTrue(ar.DesktopRuntime.catalog_stale({'modalities': loaded}, config))
        self.assertTrue(ar.DesktopRuntime.catalog_stale({'modalities': expected}, {'model_catalog_json': None}))


class DesktopRuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cursor-daemon-guard-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'cursor-fallback-state').mkdir()
        self.runtime = ar.DesktopRuntime(self.directory)
        self.snapshot = {'state': 'running', 'pid': 424200, 'start_ticks': 12345,
                         'executable': '/fixture/codex', 'active_tasks': 0}

    def test_non_default_home_mutation_rejected(self):
        with self.assertRaisesRegex(ar.RuntimeBlocked, 'effective Codex home'):
            self.runtime.mutation_guard()

    def test_proxy_presence_rejected_before_inspecting_tasks(self):
        with patch.object(self.runtime, 'mutation_guard'), patch('cursor_sdk_bridge.appserver_runtime.proxy_pids', return_value=[42]), patch.object(self.runtime, 'inspect') as inspect:
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'Disconnect'):
                self.runtime.idle()
            inspect.assert_not_called()

    def test_no_proxy_but_active_task_rejected(self):
        active = dict(self.snapshot, active_tasks=1)
        with patch.object(self.runtime, 'mutation_guard'), patch('cursor_sdk_bridge.appserver_runtime.proxy_pids', return_value=[]), patch('cursor_sdk_bridge.appserver_runtime.verify_launch'), patch.object(self.runtime, 'inspect', return_value=active):
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'active tasks'):
                self.runtime.idle()

    def test_start_never_adopts_existing_listener(self):
        with patch.object(self.runtime, 'mutation_guard'), patch.object(self.runtime, 'idle', return_value=self.snapshot), patch('cursor_sdk_bridge.appserver_runtime.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'not adopted or stopped'):
                self.runtime.start('/fixture/codex')
            popen.assert_not_called()

    def test_start_race_only_terminates_its_own_child(self):
        executable = self.directory / 'codex'
        executable.write_bytes(b'\x7fELFfixture')
        child = Mock(pid=424201)
        child.poll.return_value = None
        child.wait.return_value = -15
        foreign = dict(self.snapshot, pid=424299)
        with patch.object(self.runtime, 'mutation_guard'), patch.object(self.runtime, 'idle', return_value={'state':'absent'}), patch.object(self.runtime, 'inspect', return_value=foreign), patch('cursor_sdk_bridge.appserver_runtime.subprocess.Popen', return_value=child) as popen:
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'different daemon won'):
                self.runtime.start(str(executable))
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        self.assertEqual(popen.call_args.args[0][0], str(executable))
        opts = popen.call_args.kwargs
        self.assertEqual(opts['cwd'], Path.home())
        self.assertEqual(opts['env']['SSH_AUTH_SOCK'], str(self.runtime.socket_path.parent / 'forwarded-ssh-agent.sock'))
        for name in ('NO_PROXY', 'no_proxy'):
            self.assertTrue({'localhost','127.0.0.1','::1'}.issubset(opts['env'][name].split(',')))

    def test_pidfd_identity_mismatch_never_signals(self):
        wrong = {k: self.snapshot[k] for k in ('pid','start_ticks','executable')}
        wrong['start_ticks'] += 1
        with patch.object(self.runtime, 'idle', return_value=self.snapshot), patch('cursor_sdk_bridge.appserver_runtime.os.pidfd_open', return_value=91), patch('cursor_sdk_bridge.appserver_runtime.identity', return_value=wrong), patch('cursor_sdk_bridge.appserver_runtime.os.close') as close, patch('cursor_sdk_bridge.appserver_runtime.signal.pidfd_send_signal') as send:
            with self.assertRaisesRegex(ar.RuntimeBlocked, 'identity changed'):
                self.runtime.stop(self.snapshot)
        send.assert_not_called()
        close.assert_called_once_with(91)

    def test_stop_signals_the_same_pidfd_only(self):
        ident = {k: self.snapshot[k] for k in ('pid','start_ticks','executable')}
        with patch.object(self.runtime, 'idle', return_value=self.snapshot), patch('cursor_sdk_bridge.appserver_runtime.os.pidfd_open', return_value=91) as open_pidfd, patch('cursor_sdk_bridge.appserver_runtime.identity', return_value=ident), patch('cursor_sdk_bridge.appserver_runtime.os.close') as close, patch('cursor_sdk_bridge.appserver_runtime.signal.pidfd_send_signal') as send, patch('cursor_sdk_bridge.appserver_runtime.select.select', side_effect=[([],[],[]),([91],[],[])]):
            self.runtime.stop(self.snapshot)
        open_pidfd.assert_called_once_with(self.snapshot['pid'])
        self.assertEqual([call.args for call in send.call_args_list], [(91,signal.SIGTERM),(91,signal.SIGKILL)])
        close.assert_called_once_with(91)

    def test_legal_default_openai_model_matches(self):
        empty = {'model_provider':'openai','model':None,'model_reasoning_effort':None,'model_catalog_json':None}
        observed = {'state':'running','config':empty,'models':['gpt-6-astra'],'default_models':['gpt-6-astra']}
        self.assertTrue(ar.DesktopRuntime.matches(observed, {}))
        observed['default_models'] = []
        self.assertFalse(ar.DesktopRuntime.matches(observed, {}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
