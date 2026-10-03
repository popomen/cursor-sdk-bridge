"""Three-provider transitions with fake services and a fake consumer; no quota used."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import appserver_runtime, cli, dashboard, deployment, runtime_switch, switch_config, traex
from test_runtime_switch import FakeRuntime

CATALOG = [{'slug': model, 'input_modalities': ['text', 'image'], 'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high', 'xhigh')]}
           for model in traex.MODELS]


class ThreeProviderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.original = '# keep this comment\nmodel="gpt-6-astra"\nmodel_reasoning_effort="ultra"\nmodel_context_window=111111\n'
        (self.directory / 'config.toml').write_text(self.original)
        (self.directory / 'auth.json').write_bytes(b'fixture-credential')
        self.runtime = FakeRuntime(self.directory)
        for name in ('cursor_sdk_bridge.switch_config.verify_service', 'cursor_sdk_bridge.runtime_switch.verify_service',
                     'cursor_sdk_bridge.runtime_switch.probe', 'cursor_sdk_bridge.probe_traex.probe'):
            p = patch(name)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(traex, 'verify_service', return_value=CATALOG)
        self.verify = p.start()
        self.addCleanup(p.stop)

    def config(self):
        return tomllib.loads((self.directory / 'config.toml').read_text())

    def apply(self, provider):
        return runtime_switch.apply_mode(self.directory, provider, restart=True, runtime=self.runtime)

    def test_all_six_directions_and_original_baseline(self):
        for target in ('cursor', 'traex', 'cursor', 'openai', 'traex', 'openai'):
            with self.subTest(target=target):
                self.apply(target)
                config = self.config()
                self.assertEqual(config.get('model_provider', 'openai'), target)
                self.assertTrue(self.runtime.matches(self.runtime.inspect(), config))
                self.assertEqual((self.directory / 'auth.json').read_bytes(), b'fixture-credential')
                self.assertFalse(switch_config.status(self.directory)['transaction_pending'])
        config = self.config()
        self.assertEqual(config['model_reasoning_effort'], 'ultra')
        self.assertEqual(config['model_context_window'], 111111)
        self.assertIn('cursor', config['model_providers'])
        self.assertIn('traex', config['model_providers'])

    def test_traex_configuration_and_idempotent_effort_change(self):
        self.apply('traex')
        config = self.config()
        self.assertTrue(Path(config['model_catalog_json']).is_file())
        self.assertNotIn('model_context_window', config)
        provider = config['model_providers']['traex']
        self.assertEqual(provider['base_url'], 'http://127.0.0.1:8792/v1')
        self.assertEqual(provider['auth']['command'], '/bin/cat')
        self.assertNotIn('requires_openai_auth', provider)
        self.assertEqual(provider['stream_max_retries'], 0)
        p = self.directory / 'config.toml'
        p.write_text(p.read_text().replace('"medium"', '"xhigh"'))
        self.runtime.live = self.runtime.snapshot()
        stopped = len(self.runtime.stopped)
        self.apply('traex')
        self.assertEqual(len(self.runtime.stopped), stopped)
        self.assertEqual(self.config()['model_reasoning_effort'], 'xhigh')

    def test_v2_to_v3_migration_retains_baseline(self):
        self.apply('cursor')
        state_path = self.directory / 'cursor-fallback-state/state.json'
        before = json.loads(state_path.read_text())
        self.assertEqual(before['version'], 2)
        self.apply('traex')
        after = json.loads(state_path.read_text())
        self.assertEqual(after['version'], 3)
        self.assertEqual(after['original_config'], before['original_config'])

    def test_failed_switch_restores_previous_runtime_and_exact_files(self):
        for source, target in (('cursor', 'traex'), ('traex', 'cursor'), ('traex', 'openai')):
            with self.subTest(source=source, target=target):
                self.apply(source)
                paths = [self.directory / 'config.toml', self.directory / 'cursor-fallback-state/state.json']
                before = [p.read_bytes() for p in paths]
                self.runtime.actions = ['mismatch', 'ok']
                with self.assertRaises(appserver_runtime.RuntimeBlocked):
                    self.apply(target)
                self.assertEqual([p.read_bytes() for p in paths], before)
                self.assertEqual(self.config()['model_provider'], source)

    def test_unready_traex_does_not_stop_daemon(self):
        self.verify.side_effect = switch_config.ServiceNotReady('fixture failure')
        with self.assertRaises(switch_config.ServiceNotReady):
            self.apply('traex')
        self.assertFalse(self.runtime.stopped)
        self.assertEqual((self.directory / 'config.toml').read_text(), self.original)

    def test_external_edit_is_preserved(self):
        self.apply('traex')
        p = self.directory / 'config.toml'
        p.write_text(p.read_text().replace('http://127.0.0.1:8792/v1', 'http://127.0.0.1:9999/v1'))
        before = p.read_bytes()
        with self.assertRaises(appserver_runtime.RuntimeBlocked):
            self.apply('cursor')
        self.assertEqual(p.read_bytes(), before)

    def test_runtime_rejects_wrong_provider_catalog(self):
        self.apply('traex')
        snapshot = self.runtime.inspect()
        snapshot['models'].append('claude-opus-5-5-high')
        self.assertFalse(self.runtime.matches(snapshot, self.config()))

    def test_catalog_snapshots_are_immutable_and_verified(self):
        path = traex.catalog_snapshot(self.directory, CATALOG)
        self.assertEqual(traex.catalog_snapshot(self.directory, CATALOG), path)
        path.write_text('{}')
        with self.assertRaises(ValueError):
            traex.catalog_snapshot(self.directory, CATALOG)
        self.assertEqual(path.read_text(), '{}')

    def test_interrupted_v3_file_transaction_can_restore_openai(self):
        with patch.object(switch_config, 'finish_transaction', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError):
                switch_config.switch(self.directory, 'traex', traex.PORT)
        state = self.directory / 'cursor-fallback-state/state.json'
        self.assertIn('transaction', json.loads(state.read_text()))
        switch_config.switch(self.directory, 'openai')
        self.assertFalse(state.exists())
        self.assertEqual(self.config()['model'], 'gpt-6-astra')
        self.assertIn('traex', self.config()['model_providers'])

    def test_external_edit_during_prepare_is_never_overwritten(self):
        config = self.directory / 'config.toml'
        edit = b'model="external-user-choice"\n'
        def prepared(final, staging):
            config.write_bytes(edit)
        with self.assertRaises(ValueError):
            switch_config.switch(self.directory, 'traex', traex.PORT, prepared=prepared)
        self.assertEqual(config.read_bytes(), edit)
        self.assertFalse((self.directory / 'cursor-fallback-state/state.json').exists())

    def test_cursor_model_is_not_a_valid_user_selection_in_traex(self):
        self.apply('traex')
        p = self.directory / 'config.toml'
        p.write_text(p.read_text().replace(traex.DEFAULT_MODEL, 'claude-opus-5-5-high').replace('"medium"', '"high"'))
        self.assertTrue(switch_config.status(self.directory)['managed_config_conflict'])


class TraexOperationsTests(unittest.TestCase):
    def test_claude_traex_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['switch', 'claude', 'traex'])

    def test_authenticated_admin_request_does_not_expose_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            key.write_text('fixture-only')
            with patch.object(traex, 'KEY_FILE', key), patch('urllib.request.OpenerDirector.open') as send:
                send.return_value.__enter__.return_value.read.return_value = b'{"draining":true,"unfinished":0}'
                deployment.admin_request(traex.PORT, 'drain')
                self.assertEqual(send.call_args.args[0].get_header('Authorization'), 'Bearer fixture-only')

    def test_dashboard_does_not_require_anthropic_for_traex(self):
        spec = next(s for s in dashboard.INSTANCES if s['name'] == 'traex')
        health = {'service': 'traex-bridge', 'capabilities': spec['capabilities'], 'limits': {'queue_timeout': 100},
                  'progress': {'runs': [], 'queued': 0, 'unfinished': 0}, 'running_version': 'a' * 40}
        result = dashboard.describe(spec, health, None)
        self.assertEqual(result['state'], 'idle')
        self.assertFalse(result['notes'])

    def test_build_failure_does_not_select_or_start_services(self):
        from test_deployment import FakeCommands, extracted
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'releases'
            root.mkdir()
            def extract(repo, commit, release):
                extracted(repo, commit, release)
                source = release / 'integrations/traex'
                source.mkdir(parents=True)
                (source / 'package-lock.json').write_text('{}')
            runner = FakeCommands(fail=lambda args: args[:2] == ['npm', 'run'])
            with self.assertRaises(deployment.DeploymentError):
                deployment.install_release(tmp, 'HEAD', root, runner, extract)
            self.assertFalse(list(root.iterdir()))
            self.assertFalse(any('systemctl' in cmd for cmd in runner.calls))
