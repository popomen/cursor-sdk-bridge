import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import tomlkit
from cursor_sdk_bridge import switch_config as switcher

ORIGINAL = '''# user comment
model = "gpt-6-astra"
model_reasoning_effort = "ultra"
instructions = """multiline
[features]
model = 'inside a string'
"""
[features]
code_mode = true # keep this comment
unrelated = true
[projects."/some/path"]
trust_level = "trusted"
'''
AUTH = b'opaque-openai-auth-bytes\n'
REFRESHED_AUTH = b'refreshed-or-new-login-auth\n'
BRIDGE_PROVIDER = {
    "name": "Cursor SDK", "base_url": "http://127.0.0.1:8789/v1",
    "wire_api": "responses", "supports_websockets": False, "requires_openai_auth": False,
}


def restored_config(baseline=ORIGINAL, provider=None):
    expected = tomllib.loads(baseline.decode() if isinstance(baseline, bytes) else baseline)
    expected.setdefault("model_providers", {}).setdefault("cursor", provider or BRIDGE_PROVIDER)
    return expected


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.auth = self.root / "config.toml", self.root / "auth.json"
        self.config.write_text(ORIGINAL)
        self.auth.write_bytes(AUTH)
        self.auth.chmod(0o600)
        self.state = self.root / "cursor-fallback-state/state.json"
        self.health = patch.object(switcher, "verify_service")
        self.health.start()

    def tearDown(self):
        self.health.stop()
        self.temp.cleanup()

    def auth_stamp(self):
        info = self.auth.stat()
        return info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns

    def change_config(self, key, value):
        document = tomlkit.parse(self.config.read_text())
        document[key] = value
        self.config.write_text(tomlkit.dumps(document))

    def legacy_state(self, *, auth_present=False, transaction_mode=None, config_after=True):
        """Build v1 fixture from actual installed fields, with the old auth-removal contract."""
        baseline = self.config.read_bytes()
        switcher.switch(self.root, "cursor")
        self.change_config("cli_auth_credentials_store", "file")
        installed = self.config.read_bytes()
        state = json.loads(self.state.read_text())
        state.update(version=1, original_auth=switcher.encode(AUTH))
        state["installed"]["cli_auth_credentials_store"] = {"present": True, "value": "file"}
        if transaction_mode:
            before, after = ((baseline, installed) if transaction_mode == "cursor" else (installed, baseline))
            state["transaction"] = {
                "mode": transaction_mode,
                "config.toml": {"before": switcher.encode(before), "after": switcher.encode(after)},
                "auth.json": {"before": switcher.encode(AUTH if transaction_mode == "cursor" else None),
                              "after": switcher.encode(None if transaction_mode == "cursor" else AUTH)},
            }
            self.config.write_bytes(after if config_after else before)
        if not auth_present:
            self.auth.unlink()
        self.state.write_text(json.dumps(state))
        return baseline

    def fail_first_config_write(self, mode):
        atomic = switcher.atomic
        def fail(path, value):
            if path == self.config:
                raise OSError("simulated disk failure")
            return atomic(path, value)
        with patch.object(switcher, "atomic", side_effect=fail):
            with self.assertRaises(OSError):
                switcher.switch(self.root, mode)
        self.assertIn("transaction", json.loads(self.state.read_text()))

    def install_old_catalog(self):
        old_root = self.root / 'old-skill'
        (old_root / 'assets').mkdir(parents=True)
        (old_root / 'assets/models.json').write_bytes((switcher.ROOT / 'assets/models.json').read_bytes())
        with patch.object(switcher, 'ROOT', old_root):
            switcher.switch(self.root, 'cursor')
        return old_root / 'assets/models.json'

    def old_restore_transaction(self, *, config_after):
        """A v2 restore journal written before provider retention was introduced."""
        switcher.switch(self.root, "cursor")
        state = json.loads(self.state.read_text())
        before = self.config.read_bytes()
        after = switcher.decode(state["original_config"])
        state["transaction"] = {"mode": "openai", "config.toml": {
            "before": switcher.encode(before), "after": switcher.encode(after)}}
        self.state.write_text(json.dumps(state))
        self.config.write_bytes(after if config_after else before)

    def test_all_variants_can_be_selected_then_restored_without_conflict(self):
        from test_models import VARIANTS
        switcher.switch(self.root, "cursor")
        for alias, context, effort, fast in VARIANTS:
            self.change_config("model", alias)
            self.change_config("model_reasoning_effort", effort)
            self.assertFalse(switcher.status(self.root)["managed_config_conflict"])
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_restore_retains_only_cursor_definition_beyond_the_original_settings(self):
        self.change_config("model_catalog_json", "/original/catalog.json")
        self.change_config("web_search", "live")
        self.change_config("cli_auth_credentials_store", "keyring")
        self.change_config("model_providers", {"other": {"name": "Other provider"}})
        baseline = self.config.read_bytes()
        stamp = self.auth_stamp()
        switcher.switch(self.root, "cursor", port=18889)
        provider = {**BRIDGE_PROVIDER, "base_url": "http://127.0.0.1:18889/v1"}
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(baseline, provider))
        self.assertEqual(self.auth_stamp(), stamp)
        self.assertFalse(self.state.exists())

    def test_restore_into_empty_baseline_keeps_provider_and_default_openai(self):
        self.config.write_bytes(b"")
        switcher.switch(self.root, "cursor")
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), {"model_providers": {"cursor": BRIDGE_PROVIDER}})
        self.assertEqual(switcher.status(self.root)["provider"], "openai")

    def test_original_cursor_definition_is_restored_with_comments_and_nested_settings(self):
        baseline = ORIGINAL + '''
[model_providers.cursor] # original provider
name = "Original Cursor"
base_url = "http://127.0.0.1:18888/v1"
wire_api = "responses"
requires_openai_auth = true
[model_providers.cursor.http_headers]
X-Test = "original-header-value"
'''
        self.config.write_text(baseline)
        switcher.switch(self.root, "cursor")
        self.assertEqual(tomllib.loads(self.config.read_text())["model_providers"]["cursor"], BRIDGE_PROVIDER)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(baseline))
        self.assertIn("# original provider", self.config.read_text())

    def test_repeated_cycles_preserve_the_first_retained_definition(self):
        for port in (8789, 18889, 28889):
            with self.subTest(port=port):
                switcher.switch(self.root, "cursor", port=port)
                self.assertEqual(tomllib.loads(self.config.read_text())["model_providers"]["cursor"]["base_url"],
                                 f"http://127.0.0.1:{port}/v1")
                switcher.switch(self.root, "openai")
                self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
                restored = self.config.read_bytes()
                switcher.switch(self.root, "openai")
                self.assertEqual(self.config.read_bytes(), restored)

    def test_removing_active_cursor_definition_is_an_external_conflict(self):
        switcher.switch(self.root, "cursor")
        self.change_config("model_providers", {})
        before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
        with self.assertRaisesRegex(ValueError, "changed externally"):
            switcher.switch(self.root, "openai")
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)

    def test_old_restore_transaction_registers_upgrade_before_mutation(self):
        for config_after in (False, True):
            with self.subTest(config_after=config_after):
                self.config.write_text(ORIGINAL)
                self.old_restore_transaction(config_after=config_after)
                before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
                prepared = []
                def record(final, staging):
                    self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)
                    prepared.append((final, staging))
                switcher.switch(self.root, "openai", prepared=record)
                self.assertEqual(len(prepared), 1)
                final, staging = prepared[0]
                transaction = json.loads(staging)["transaction"]["config.toml"]
                self.assertEqual(switcher.decode(transaction["before"]), before[0])
                self.assertEqual(switcher.decode(transaction["after"]), final["config"])
                self.assertEqual(final, {"config": self.config.read_bytes(), "state": None})
                self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
                self.assertFalse(self.state.exists())
                self.assertEqual(self.auth_stamp(), before[2])

    def test_old_restore_upgrade_recovers_after_config_write_or_finalization_failure(self):
        for config_after in (False, True):
            for fail_config in (False, True):
                with self.subTest(config_after=config_after, fail_config=fail_config):
                    self.config.write_text(ORIGINAL)
                    self.old_restore_transaction(config_after=config_after)
                    before_config = self.config.read_bytes()
                    atomic = switcher.atomic
                    def fail(path, value):
                        if (fail_config and path == self.config) or (not fail_config and path == self.state and value is None):
                            raise OSError("simulated write failure")
                        return atomic(path, value)
                    with patch.object(switcher, "atomic", side_effect=fail):
                        with self.assertRaises(OSError):
                            switcher.switch(self.root, "openai")
                    transaction = json.loads(self.state.read_text())["transaction"]["config.toml"]
                    self.assertEqual(switcher.decode(transaction["before"]), before_config)
                    self.assertEqual(tomllib.loads(switcher.decode(transaction["after"]).decode()), restored_config())
                    self.auth.write_bytes(REFRESHED_AUTH)
                    stamp = self.auth_stamp()
                    switcher.switch(self.root, "openai")
                    self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
                    self.assertEqual(self.auth_stamp(), stamp)
                    self.assertFalse(self.state.exists())

    def test_old_restore_upgrade_refuses_preparation_edits_to_config_or_state(self):
        for config_after in (False, True):
            for target in (self.config, self.state):
                with self.subTest(config_after=config_after, target=target.name):
                    self.config.write_text(ORIGINAL)
                    self.state.unlink(missing_ok=True)
                    self.old_restore_transaction(config_after=config_after)
                    before = self.config.read_bytes(), self.state.read_bytes()
                    def edit(final, staging):
                        target.write_bytes(target.read_bytes() + b"\n")
                    with self.assertRaisesRegex(ValueError, "external changes|changed during switch preparation"):
                        switcher.switch(self.root, "openai", prepared=edit)
                    expected = list(before)
                    expected[0 if target == self.config else 1] += b"\n"
                    self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), tuple(expected))
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_old_restore_upgrade_refuses_unowned_config_before_preparation(self):
        self.old_restore_transaction(config_after=True)
        self.config.write_bytes(self.config.read_bytes() + b"# external edit\n")
        before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
        with patch.object(switcher, "write_state") as write, self.assertRaisesRegex(ValueError, "external changes"):
            switcher.switch(self.root, "openai", prepared=lambda *_: self.fail("must validate before preparation"))
        write.assert_not_called()
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)

    def test_old_restore_upgrade_state_write_failure_leaves_original_owned_bytes(self):
        self.old_restore_transaction(config_after=True)
        before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
        prepared = []
        with patch.object(switcher, "write_state", side_effect=OSError("simulated journal failure")):
            with self.assertRaises(OSError):
                switcher.switch(self.root, "openai", prepared=lambda *values: prepared.append(values))
        self.assertEqual(len(prepared), 1)
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())

    def test_current_restore_transaction_reuses_its_exact_staged_bytes(self):
        switcher.switch(self.root, "cursor")
        self.fail_first_config_write("openai")
        # Ownership includes serialization, not just the parsed state values.
        self.state.write_text(json.dumps(json.loads(self.state.read_text()), separators=(",", ":")) + "\n")
        staged = self.state.read_bytes()
        prepared = []
        with patch.object(switcher, "write_state") as write:
            switcher.switch(self.root, "openai", prepared=lambda *values: prepared.append(values))
        write.assert_not_called()
        self.assertEqual(prepared, [({"config": self.config.read_bytes(), "state": None}, staged)])
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())

    def test_legacy_missing_table_uses_installed_snapshot_and_prepares_before_writes(self):
        self.legacy_state(auth_present=True, transaction_mode="openai", config_after=True)
        self.assertNotIn("model_providers", tomllib.loads(self.config.read_text()))
        state = json.loads(self.state.read_text())
        provider = {**BRIDGE_PROVIDER, "base_url": "http://127.0.0.1:18889/v1"}
        state["installed"]["model_providers.cursor"]["value"] = provider
        self.state.write_text(json.dumps(state))
        before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
        prepared = []
        def record(final, staging):
            self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)
            prepared.append((final, staging))
        switcher.switch(self.root, "openai", prepared=record)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0][0], {"config": self.config.read_bytes(), "state": None})
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(provider=provider))
        self.assertEqual(self.auth_stamp(), before[2])

    def test_catalog_upgrade_status_is_readonly_and_gives_independent_terminal_command(self):
        old = self.install_old_catalog()
        before = self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()
        status = switcher.status(self.root)
        self.assertTrue(status['catalog_path_upgrade_required'])
        self.assertFalse(status['managed_config_conflict'])
        self.assertEqual(status['model_catalog_json'], str(old))
        self.assertEqual(status['expected_model_catalog_json'], str(switcher.ROOT / 'assets/models.json'))
        self.assertIn('cursor-sdk-bridge switch codex cursor --restart-daemon', status['next_step'])
        self.assertIn('separate SSH terminal', status['next_step'])
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes(), self.auth_stamp()), before)

    def test_catalog_upgrade_is_one_transaction_preserving_baseline_effort_and_auth(self):
        self.install_old_catalog()
        self.change_config('model', 'claude-opus-5-5-max')
        self.change_config('model_reasoning_effort', 'max')
        self.change_config('unrelated_setting', 'preserved')
        old_config = self.config.read_bytes()
        old_state = json.loads(self.state.read_text())
        stamp = self.auth_stamp()
        prepared = []
        switcher.switch(self.root, 'cursor', prepared=lambda final, staging: prepared.append((final, staging)))
        config = tomllib.loads(self.config.read_text())
        state = json.loads(self.state.read_text())
        self.assertEqual(config['model_catalog_json'], str(switcher.ROOT / 'assets/models.json'))
        self.assertEqual((config['model'], config['model_reasoning_effort']), ('claude-opus-5-5-max', 'max'))
        self.assertEqual(state['original_config'], old_state['original_config'])
        unchanged = {key: value for key, value in state['installed'].items() if key != 'model_catalog_json'}
        self.assertEqual(unchanged, {key: value for key, value in old_state['installed'].items() if key != 'model_catalog_json'})
        self.assertEqual(state['installed']['model_catalog_json'], {'present': True, 'value': config['model_catalog_json']})
        self.assertEqual(len(prepared), 1)
        final, staging = prepared[0]
        self.assertEqual((final['config'], final['state']), (self.config.read_bytes(), self.state.read_bytes()))
        transaction = json.loads(staging)['transaction']
        self.assertEqual(switcher.decode(transaction['config.toml']['before']), old_config)
        self.assertEqual(switcher.decode(transaction['config.toml']['after']), self.config.read_bytes())
        self.assertFalse(switcher.status(self.root)['catalog_path_upgrade_required'])
        before = self.config.read_bytes(), self.state.read_bytes()
        switcher.switch(self.root, 'cursor')
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)
        switcher.switch(self.root, 'openai')
        restored = tomllib.loads(self.config.read_text())
        self.assertEqual(restored.pop('unrelated_setting'), 'preserved')
        self.assertEqual(restored, restored_config())
        self.assertEqual(self.auth_stamp(), stamp)

    def test_interrupted_catalog_upgrade_recovers_before_config_write(self):
        self.install_old_catalog()
        original_baseline = json.loads(self.state.read_text())['original_config']
        self.fail_first_config_write('cursor')
        self.assertTrue(switcher.status(self.root)['transaction_pending'])
        switcher.switch(self.root, 'cursor')
        self.assertFalse(switcher.status(self.root)['catalog_path_upgrade_required'])
        self.assertFalse(switcher.status(self.root)['transaction_pending'])
        self.assertEqual(json.loads(self.state.read_text())['original_config'], original_baseline)
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_interrupted_catalog_upgrade_preserves_state_edited_during_recovery_prepare(self):
        self.install_old_catalog()
        self.fail_first_config_write('cursor')
        before = self.config.read_bytes(), self.state.read_bytes()
        def edit(final, staging):
            self.state.write_bytes(self.state.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'state conflicts with external changes'):
            switcher.switch(self.root, 'cursor', prepared=edit)
        self.assertEqual(self.config.read_bytes(), before[0])
        self.assertEqual(self.state.read_bytes(), before[1] + b'\n')
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_catalog_upgrade_external_managed_path_is_not_overwritten(self):
        self.install_old_catalog()
        self.change_config('model_catalog_json', '/external/catalog.json')
        before = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed externally'):
            switcher.switch(self.root, 'cursor')
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)

    def test_catalog_upgrade_preserves_config_or_state_edited_during_prepare(self):
        self.install_old_catalog()
        for target in (self.state, self.config):
            with self.subTest(target=target.name):
                before = self.config.read_bytes(), self.state.read_bytes()
                def edit(final, staging):
                    target.write_bytes(target.read_bytes() + b'\n')
                with self.assertRaisesRegex(ValueError, 'changed during switch preparation'):
                    switcher.switch(self.root, 'cursor', prepared=edit)
                self.assertEqual(target.read_bytes(), before[1 if target == self.state else 0] + b'\n')
                other = self.config if target == self.state else self.state
                self.assertEqual(other.read_bytes(), before[0 if target == self.state else 1])
                target.write_bytes(before[1 if target == self.state else 0])
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_roundtrip_preserves_auth_inode_bytes_permissions_and_unrelated_config(self):
        original_auth_stat = self.auth_stamp()
        switcher.switch(self.root, "cursor")
        active = tomllib.loads(self.config.read_text())
        self.assertEqual(active["model_provider"], "cursor")
        self.assertEqual(self.auth.read_bytes(), AUTH)
        self.assertEqual(self.auth_stamp(), original_auth_stat)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["version"], 2)
        self.assertNotIn("original_auth", state)
        self.assertNotIn(switcher.encode(AUTH), self.state.read_text())
        self.config.write_text(self.config.read_text().replace('trust_level = "trusted"', 'trust_level = "untrusted"'))
        switcher.switch(self.root, "openai")
        expected = restored_config(ORIGINAL.replace('trust_level = "trusted"', 'trust_level = "untrusted"'))
        self.assertEqual(tomllib.loads(self.config.read_text()), expected)
        self.assertIn("# user comment", self.config.read_text())
        self.assertIn("# keep this comment", self.config.read_text())
        self.assertEqual(self.auth_stamp(), original_auth_stat)
        self.assertFalse(self.state.exists())

    def test_refresh_or_login_during_cursor_is_preserved_and_not_a_conflict(self):
        switcher.switch(self.root, "cursor")
        self.auth.write_bytes(REFRESHED_AUTH)
        auth_stat = self.auth_stamp()
        switcher.switch(self.root, "cursor")
        self.assertFalse(switcher.status(self.root)["managed_config_conflict"])
        switcher.switch(self.root, "openai")
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        self.assertEqual(self.auth_stamp(), auth_stat)

    def test_missing_auth_stays_missing(self):
        self.auth.unlink()
        switcher.switch(self.root, "cursor")
        switcher.switch(self.root, "openai")
        self.assertFalse(self.auth.exists())

    def test_new_auth_from_missing_baseline_is_preserved(self):
        self.auth.unlink()
        switcher.switch(self.root, "cursor")
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)

    def test_logout_during_cursor_is_not_undone(self):
        switcher.switch(self.root, "cursor")
        self.auth.unlink()
        switcher.switch(self.root, "openai")
        self.assertFalse(self.auth.exists())

    def test_credentials_store_is_not_owned_by_v2(self):
        for store in (None, "file", "auto", "keyring", "ephemeral"):
            with self.subTest(store=store):
                self.config.write_text(ORIGINAL)
                if store is not None:
                    self.change_config("cli_auth_credentials_store", store)
                switcher.switch(self.root, "cursor")
                active = tomllib.loads(self.config.read_text())
                self.assertEqual(active.get("cli_auth_credentials_store"), store)
                self.assertNotIn("cli_auth_credentials_store", json.loads(self.state.read_text())["installed"])
                switcher.switch(self.root, "openai")
                self.assertEqual(tomllib.loads(self.config.read_text()).get("cli_auth_credentials_store"), store)

    def test_external_credentials_store_change_survives_v2_roundtrip(self):
        self.change_config("cli_auth_credentials_store", "file")
        switcher.switch(self.root, "cursor")
        self.change_config("cli_auth_credentials_store", "keyring")
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text())["cli_auth_credentials_store"], "keyring")

    def test_existing_off_file_untouched(self):
        off = self.root / "auth.json.off"
        off.write_bytes(b'unrelated')
        for mode in ("cursor", "openai"):
            switcher.switch(self.root, mode)
        self.assertEqual(off.read_bytes(), b'unrelated')

    def test_idempotent_and_new_cycle_captures_new_baseline(self):
        for mode in ("cursor", "cursor", "openai", "openai"):
            switcher.switch(self.root, mode)
        self.change_config("model", "gpt-new")
        switcher.switch(self.root, "cursor")
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text())["model"], "gpt-new")

    def test_supported_effort_selection_restores_openai_baseline(self):
        for effort in ("xhigh", "max"):
            switcher.switch(self.root, "cursor")
            self.change_config("model", "claude-opus-5-5-" + effort)
            self.change_config("model_reasoning_effort", effort)
            self.assertFalse(switcher.status(self.root)["managed_config_conflict"])
            switcher.switch(self.root, "cursor")
            switcher.switch(self.root, "openai")
            self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())

    def test_managed_configuration_conflict_preserves_both_sides(self):
        switcher.switch(self.root, "cursor")
        self.change_config("model", "unknown-model")
        before_config, before_state = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "openai")
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_config_write_failure_does_not_touch_auth_and_recovery_accepts_refresh(self):
        self.fail_first_config_write("cursor")
        self.assertEqual(self.auth.read_bytes(), AUTH)
        transaction = json.loads(self.state.read_text())["transaction"]
        self.assertEqual(set(transaction), {"mode", "config.toml"})
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        self.assertFalse(self.state.exists())

    def test_recovery_after_config_written_before_transaction_finalization(self):
        atomic = switcher.atomic
        state_writes = 0
        def fail_finalize(path, value):
            nonlocal state_writes
            if path == self.state:
                state_writes += 1
                if state_writes == 2:
                    raise OSError("simulated finalize failure")
            return atomic(path, value)
        with patch.object(switcher, "atomic", side_effect=fail_finalize):
            with self.assertRaises(OSError):
                switcher.switch(self.root, "cursor")
        self.assertEqual(tomllib.loads(self.config.read_text())["model_provider"], "cursor")
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "cursor")
        self.assertNotIn("transaction", json.loads(self.state.read_text()))
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())

    def test_restore_transaction_recovery_preserves_refreshed_auth(self):
        switcher.switch(self.root, "cursor")
        self.fail_first_config_write("openai")
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config())
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        self.assertFalse(self.state.exists())

    def test_transaction_recovery_refuses_external_config_changes(self):
        self.fail_first_config_write("cursor")
        self.config.write_text('model = "external"\n')
        before_state = self.state.read_bytes()
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "openai")
        self.assertEqual(self.config.read_text(), 'model = "external"\n')
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(self.auth.read_bytes(), AUTH)

    def test_legacy_restore_recovers_missing_auth_and_original_store(self):
        self.change_config("cli_auth_credentials_store", "auto")
        baseline = self.legacy_state()
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(baseline))
        self.assertEqual(self.auth.read_bytes(), AUTH)
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.state.exists())

    def test_legacy_restore_does_not_overwrite_existing_login(self):
        baseline = self.legacy_state(auth_present=True)
        self.auth.write_bytes(REFRESHED_AUTH)
        auth_stat = self.auth_stamp()
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(baseline))
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        self.assertEqual(self.auth_stamp(), auth_stat)

    def test_legacy_without_auth_backup_does_not_invent_credentials(self):
        self.legacy_state()
        state = json.loads(self.state.read_text())
        state["original_auth"] = None
        self.state.write_text(json.dumps(state))
        switcher.switch(self.root, "openai")
        self.assertFalse(self.auth.exists())

    def test_legacy_requires_recovery_before_another_cursor_switch(self):
        self.legacy_state()
        before = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "cursor")
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)
        self.assertFalse(self.auth.exists())

    def test_legacy_interrupted_transactions_recover_from_before_and_after_config(self):
        for mode in ("cursor", "openai"):
            for config_after in (False, True):
                for auth_present in (False, True):
                    with self.subTest(mode=mode, config_after=config_after, auth_present=auth_present):
                        self.config.write_text(ORIGINAL)
                        self.auth.write_bytes(AUTH)
                        baseline = self.legacy_state(auth_present=auth_present, transaction_mode=mode,
                                                     config_after=config_after)
                        if auth_present:
                            self.auth.write_bytes(REFRESHED_AUTH)
                        switcher.switch(self.root, "openai")
                        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(baseline))
                        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH if auth_present else AUTH)
                        self.assertFalse(self.state.exists())

    def test_legacy_external_managed_change_is_not_overwritten(self):
        self.legacy_state()
        self.change_config("model_provider", "external")
        before = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "openai")
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)
        self.assertFalse(self.auth.exists())

    def test_legacy_auth_recovery_survives_state_write_failure(self):
        baseline = self.legacy_state()
        with patch.object(switcher, "write_state", side_effect=OSError("simulated journal failure")):
            with self.assertRaises(OSError):
                switcher.switch(self.root, "openai")
        self.assertEqual(self.auth.read_bytes(), AUTH)
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)
        self.assertEqual(tomllib.loads(self.config.read_text()), restored_config(baseline))

    def test_status_is_readonly_and_reports_preservation(self):
        before = sorted(self.root.iterdir())
        status = switcher.status(self.root)
        self.assertEqual(status["provider"], "openai")
        self.assertEqual(status["auth_policy"], "preserved")
        self.assertFalse(status["openai_config_backup_present"])
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_managed_config_symlink_is_refused(self):
        other = self.root / "other"
        other.write_text(ORIGINAL)
        self.config.unlink()
        self.config.symlink_to(other)
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "cursor")
        self.assertEqual(other.read_text(), ORIGINAL)


    def test_legacy_auth_restore_does_not_overwrite_login_created_after_missing_check(self):
        self.legacy_state()
        original_read = switcher.read
        injected = False
        def login_races_with_missing_check(path):
            nonlocal injected
            current = original_read(path)
            if path == self.auth and current is None and not injected:
                injected = True
                self.auth.write_bytes(REFRESHED_AUTH)
            return current
        with patch.object(switcher, "read", side_effect=login_races_with_missing_check):
            switcher.switch(self.root, "openai")
        self.assertTrue(injected)
        self.assertEqual(self.auth.read_bytes(), REFRESHED_AUTH)

    def test_legacy_credentials_store_conflict_preserves_external_choice(self):
        self.change_config("cli_auth_credentials_store", "auto")
        self.legacy_state()
        self.change_config("cli_auth_credentials_store", "keyring")
        before_config, before_state = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaises(ValueError):
            switcher.switch(self.root, "openai")
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertFalse(self.auth.exists())


if __name__ == "__main__":
    unittest.main()
