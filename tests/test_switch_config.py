import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import tomlkit
from cursor_bridge import switch_config as switcher

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
        expected = tomllib.loads(ORIGINAL.replace('trust_level = "trusted"', 'trust_level = "untrusted"'))
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
            self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(ORIGINAL))

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
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(ORIGINAL))
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
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(ORIGINAL))

    def test_restore_transaction_recovery_preserves_refreshed_auth(self):
        switcher.switch(self.root, "cursor")
        self.fail_first_config_write("openai")
        self.auth.write_bytes(REFRESHED_AUTH)
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(ORIGINAL))
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
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(baseline.decode()))
        self.assertEqual(self.auth.read_bytes(), AUTH)
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.state.exists())

    def test_legacy_restore_does_not_overwrite_existing_login(self):
        baseline = self.legacy_state(auth_present=True)
        self.auth.write_bytes(REFRESHED_AUTH)
        auth_stat = self.auth_stamp()
        switcher.switch(self.root, "openai")
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(baseline.decode()))
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
                        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(baseline.decode()))
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
        self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(baseline.decode()))

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
