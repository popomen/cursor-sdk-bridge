from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cursor_bridge.sdk_support import PROXY_VARS, bridge_command, model_identity, snapshot


class SDKSupportTests(unittest.TestCase):
    def test_model_identity_compares_parameter_values_without_order(self):
        one = {'id': 'opus', 'params': [{'id': 'effort', 'value': 'high'}, {'id': 'context', 'value': '1m'}]}
        two = {'id': 'opus', 'params': list(reversed(one['params']))}
        self.assertEqual(model_identity(one), model_identity(two))
        self.assertNotEqual(model_identity(one), model_identity({'id': 'opus', 'params': [{'id': 'effort', 'value': 'max'}]}))

    def test_workspace_snapshot_detects_content_and_symlink_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'directory').mkdir()
            (root / 'file').write_text('before')
            (root / 'link').symlink_to('file')
            before = snapshot(root)
            (root / 'file').write_text('after')
            self.assertNotEqual(snapshot(root), before)
            (root / 'file').write_text('before')
            self.assertEqual(snapshot(root), before)
            (root / 'link').unlink()
            (root / 'link').symlink_to('missing')
            self.assertNotEqual(snapshot(root), before)

    def test_bridge_command_owns_node_and_clears_proxy_variables(self):
        with patch('cursor_sdk._vendor.resolve_bridge_path', return_value='/bundle/bin/bridge'), \
                patch('cursor_bridge.sdk_support.proxychains_binary', return_value='/private/proxychains4'):
            command = bridge_command('proxychains', 'http1')
            prefix = ['env'] + [part for name in PROXY_VARS for part in ('-u', name)]
            self.assertEqual(command[:len(prefix)], prefix)
            self.assertEqual(command[len(prefix):len(prefix) + 4], ['/private/proxychains4', '-q', '/bundle/bin/node', '--import'])
            self.assertTrue(command[-2].endswith('/sdk_http1.mjs'))
            self.assertEqual(command[-1], '/bundle/dist/bin/cursor-sdk-bridge.js')
            self.assertEqual(bridge_command('ambient', 'default'), ['/bundle/bin/node', '/bundle/dist/bin/cursor-sdk-bridge.js'])


if __name__ == '__main__':
    unittest.main()
