import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from cursor_bridge import cli


class CLITests(unittest.TestCase):
    def invoked(self, arguments, target):
        result = []
        before = sys.argv
        with patch(target, side_effect=lambda: result.extend(sys.argv[1:])), contextlib.redirect_stderr(io.StringIO()):
            cli.main(arguments)
        self.assertIs(sys.argv, before)
        return result

    def test_serve_passes_mode_port_and_claude_defaults(self):
        self.assertEqual(self.invoked(['serve', '--mode', 'reuse', '--port', '9890'],
                                     'cursor_bridge.cursor_sdk2api.main'), ['--mode', 'reuse', '--port', '9890'])
        args = self.invoked(['serve', 'claude', '--mode', 'legacy'], 'cursor_bridge.cursor_sdk2api.main')
        self.assertEqual(args[:2], ['--port', '8790'])
        self.assertIn('1800', args)
        self.assertEqual(args[-2:], ['--mode', 'legacy'])
        self.assertEqual(self.invoked(['serve', 'dashboard', '--port', '9891'], 'cursor_bridge.dashboard.main'),
                         ['--port', '9891'])

    def test_switch_maps_restore_and_passes_daemon_flag(self):
        self.assertEqual(self.invoked(['switch', 'codex', 'restore', '--restart-daemon'],
                                     'cursor_bridge.switch_config.main'), ['openai', '--restart-daemon'])
        self.assertEqual(self.invoked(['switch', 'claude', 'cursor', '--port', '9890'],
                                     'cursor_bridge.claude_switch.main'), ['cursor', '--port', '9890'])

    def test_probe_passthrough(self):
        self.assertEqual(self.invoked(['probe', '--messages', '--port', '9890'], 'cursor_bridge.probe_service.main'),
                         ['--messages', '--port', '9890'])

    def test_status_never_deploys_or_switches(self):
        output = io.StringIO()
        with patch.object(cli, 'status', return_value={'instances': []}), contextlib.redirect_stdout(output):
            cli.main(['status'])
        self.assertEqual(output.getvalue(), '{"instances": []}\n')

    def test_deploy_commit_and_install_only(self):
        with patch('cursor_bridge.deployment.deploy', return_value={'installed': True}) as deploy, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['deploy', 'abc123', '--install-only'])
        self.assertEqual(deploy.call_args.kwargs['commit'], 'abc123')
        self.assertTrue(deploy.call_args.kwargs['install_only'])

    def test_restart_refusal_has_nonzero_exit(self):
        with patch('cursor_bridge.dashboard.Dashboard') as board, contextlib.redirect_stdout(io.StringIO()):
            board.return_value.restart.return_value = {'result': 'refused'}
            with self.assertRaises(SystemExit) as error:
                cli.main(['restart', 'claude'])
        self.assertEqual(error.exception.code, 1)
        board.return_value.restart.assert_called_once_with('claude')


if __name__ == '__main__':
    unittest.main()
