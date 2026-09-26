import io
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import deployment, version

SHA = 'a' * 40
OLD = 'b' * 40


class FakeCommands:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, arguments, **kwargs):
        args = [str(arg) for arg in arguments]
        self.calls.append(args)
        if self.fail and self.fail(args):
            raise deployment.DeploymentError('synthetic failure')
        if args[0] == 'git':
            return subprocess.CompletedProcess(args, 0, SHA + '\n', '')
        if args[1:3] == ['-m', 'venv']:
            (Path(args[3]) / 'bin').mkdir(parents=True)
            (Path(args[3]) / 'bin/python').touch()
        return subprocess.CompletedProcess(args, 0, 'cursor-sdk==1.0.32\n', '')


def extracted(repo, commit, release):
    (release / 'cursor_sdk_bridge').mkdir()
    (release / 'cursor_sdk_bridge/cli.py').touch()
    (release / 'pyproject.toml').write_text('[project]\nname="cursor-sdk-bridge"\n')


class DeployTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root, self.units, self.bin = (self.base / name for name in ('releases', 'units', 'bin'))
        self.runner = FakeCommands()

    def deploy(self, **overrides):
        return deployment.deploy(repo=self.base, root=self.root, unit_dir=self.units, bin_dir=self.bin,
                                 runner=self.runner, extractor=extracted, **overrides)

    def test_install_only_has_no_runtime_or_systemd_mutation(self):
        result = self.deploy(install_only=True)
        self.assertTrue(result['installed'])
        self.assertFalse(result['selected'])
        self.assertFalse(self.units.exists())
        self.assertFalse((self.root / 'current').exists())
        self.assertFalse(any(args[0] == 'systemctl' for args in self.runner.calls))
        self.assertEqual(version.manifest_version(self.root / SHA / version.MANIFEST), SHA)
        self.assertEqual((self.root / SHA / 'installed-dependencies.txt').stat().st_mode & 0o777, 0o600)

    def test_publish_enables_three_regular_units_without_starting(self):
        result = self.deploy()
        self.assertEqual((result['selected'], result['units_enabled'], result['services_restarted']), (True, True, False))
        self.assertEqual((self.root / 'current').resolve(), self.root / SHA)
        self.assertEqual(version.deployed_version(self.root), SHA)
        self.assertTrue((self.bin / 'cursor-sdk-bridge').is_symlink())
        self.assertEqual(sorted(path.name for path in self.units.iterdir()),
                         sorted(deployment.unit_name(name) for name in deployment.INSTANCES))
        for path in self.units.iterdir():
            self.assertFalse(path.is_symlink())
            body = path.read_text()
            self.assertIn('WantedBy=default.target', body)
            self.assertIn(SHA + '/venv/bin/python', body)
            self.assertNotIn('/current/', body)
            self.assertNotIn('cursor-sdk2api.service', body)
            self.assertNotIn('source/', body)
        systemd = [args for args in self.runner.calls if args[0] == 'systemctl']
        self.assertEqual(systemd[0], ['systemctl', '--user', 'daemon-reload'])
        self.assertEqual(systemd[1][2], 'enable')
        self.assertFalse(any(word in ('--now', 'start', 'restart', 'stop') for args in systemd for word in args))

    def test_install_is_idempotent_and_resolves_package_dependencies_once(self):
        self.deploy(install_only=True)
        first = len(self.runner.calls)
        self.deploy(install_only=True)
        self.assertEqual(len(self.runner.calls), first + 1)
        install = [args for args in self.runner.calls if 'install' in args]
        self.assertEqual(len(install), 1)
        self.assertEqual(install[0][-1], str(self.root / SHA))
        self.assertNotIn('--no-deps', install[0])

    def test_failed_install_preserves_current_and_removes_only_new_release(self):
        self.root.mkdir()
        (self.root / OLD).mkdir()
        (self.root / 'current').symlink_to(OLD)
        self.runner.fail = lambda args: 'install' in args
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertEqual((self.root / 'current').resolve(), self.root / OLD)
        self.assertFalse((self.root / SHA).exists())

    def test_registration_failure_restores_previous_current_and_units(self):
        self.root.mkdir()
        (self.root / OLD).mkdir()
        (self.root / 'current').symlink_to(OLD)
        self.units.mkdir()
        original = deployment.MANAGED + 'original unit\n'
        unit = self.units / deployment.unit_name('codex')
        unit.write_text(original)
        self.runner.fail = lambda args: 'enable' in args
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertEqual((self.root / 'current').resolve(), self.root / OLD)
        self.assertEqual(unit.read_text(), original)
        self.assertEqual(len(list(self.units.iterdir())), 1)
        self.assertFalse((self.bin / 'cursor-sdk-bridge').is_symlink())

    def test_invalid_units_restore_previous_selection_before_enabling(self):
        self.root.mkdir()
        (self.root / OLD).mkdir()
        (self.root / 'current').symlink_to(OLD)
        self.units.mkdir()
        unit = self.units / deployment.unit_name('codex')
        original = deployment.MANAGED + 'original unit\n'
        unit.write_text(original)
        self.runner.fail = lambda args: args[0] == 'systemd-analyze'
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertEqual((self.root / 'current').resolve(), self.root / OLD)
        self.assertEqual(unit.read_text(), original)
        self.assertFalse((self.bin / 'cursor-sdk-bridge').is_symlink())
        self.assertFalse(any('enable' in args for args in self.runner.calls))

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze is unavailable')
    def test_real_systemd_parser_accepts_generated_units(self):
        release = self.base / 'release with spaces %value'
        executable = release / 'venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
        units = []
        for instance in deployment.INSTANCES:
            unit = self.base / deployment.unit_name(instance)
            unit.write_text(deployment.unit_text(instance, self.base, release))
            units.append(str(unit))
        result = subprocess.run(['systemd-analyze', '--user', 'verify', *units],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_enable_failure_removes_only_new_enabled_links(self):
        original_runner = self.runner
        self.units.mkdir()
        wants = self.units / 'default.target.wants'
        wants.mkdir()
        old_link = wants / deployment.unit_name('codex')
        old_link.symlink_to(self.units / old_link.name)
        def runner(args, **kwargs):
            if 'enable' in args:
                link = wants / deployment.unit_name('claude')
                link.symlink_to(self.units / link.name)
                raise deployment.DeploymentError('partial enable')
            return original_runner(args, **kwargs)
        self.runner = runner
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertTrue(old_link.is_symlink())
        self.assertFalse((wants / deployment.unit_name('claude')).is_symlink())

    def test_unrelated_unit_and_launcher_are_not_overwritten(self):
        self.units.mkdir()
        target = self.units / deployment.unit_name('codex')
        target.write_text('belongs to user')
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertEqual(target.read_text(), 'belongs to user')
        self.assertFalse((self.root / 'current').exists())
        target.unlink()
        self.bin.mkdir(exist_ok=True)
        launcher = self.bin / 'cursor-sdk-bridge'
        launcher.write_text('user launcher')
        with self.assertRaises(deployment.DeploymentError):
            self.deploy()
        self.assertEqual(launcher.read_text(), 'user launcher')

    def test_unit_paths_escape_systemd_specifiers_and_quotes(self):
        body = deployment.unit_text('codex', Path('/tmp/space here/%root'))
        self.assertIn('"/tmp/space here/%%root/current/venv/bin/python"', body)
        self.assertIn('WorkingDirectory=/tmp/space here/%%root/current\n', body)
        self.assertIn('UnsetEnvironment=PYTHONPATH PYTHONHOME', body)
        with self.assertRaises(deployment.DeploymentError):
            deployment.unit_text('codex', Path('/tmp/new\nline'))


class VersionTests(unittest.TestCase):
    def test_release_version_is_read_without_accessing_git_and_is_fixed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / 'cursor_sdk_bridge'
            package.mkdir()
            manifest = root / version.MANIFEST
            manifest.write_text(json.dumps({'commit': SHA}))
            version.running_version.cache_clear()
            try:
                with patch.object(version, '__file__', str(package / 'version.py')), patch.object(version.subprocess, 'run') as run:
                    self.assertEqual(version.running_version(), SHA)
                    manifest.write_text(json.dumps({'commit': OLD}))
                    self.assertEqual(version.running_version(), SHA)
                    run.assert_not_called()
            finally:
                version.running_version.cache_clear()

    def test_invalid_or_missing_manifest_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manifest'
            self.assertIsNone(version.manifest_version(path))
            path.write_text('{"commit":"secret-not-a-version"}')
            self.assertIsNone(version.manifest_version(path))


if __name__ == '__main__':
    unittest.main()
