import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cursor_sdk_bridge import claude_switch as switcher
from cursor_sdk_bridge.probe_service import ProbeFailed

REAL_VERIFY = switcher.verify_service
REAL_REPORT = switcher.service_report

SECRET = "relay-secret-token-value"
ORIGINAL = {
    "env": {"ANTHROPIC_BASE_URL": "https://relay.example", "ANTHROPIC_AUTH_TOKEN": SECRET,
            "ANTHROPIC_MODEL": "relay/flash", "ANTHROPIC_DEFAULT_OPUS_MODEL": "relay/large",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "relay/large", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "relay/flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "relay/flash", "CLAUDE_CODE_EFFORT_LEVEL": "max",
            "NO_PROXY": "relay.example,.internal", "no_proxy": "relay.example,.internal"},
    "model": "relay/flash", "availableModels": ["relay/large", "relay/flash"], "theme": "dark",
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify", "timeout": 60}]}]}}


class ClaudeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings, self.state = root / "claude/settings.json", root / "state/claude-code.json"
        self.settings.parent.mkdir()
        self.write(ORIGINAL)
        self.settings.chmod(0o600)
        self.probes = []
        for item in (patch.object(switcher, "verify_service"),
                     patch.object(switcher, "service_report", return_value={"state": "stubbed"}),
                     patch.object(switcher, "probe_messages",
                                  side_effect=lambda port, effort: self.probes.append((port, effort)))):
            item.start()
            self.addCleanup(item.stop)

    def write(self, value):
        self.settings.write_text(json.dumps(value, indent=2) + "\n")

    def load(self):
        return json.loads(self.settings.read_text())

    def switch(self, mode):
        return switcher.switch(self.settings, self.state, mode, port=8790)

    def test_catalog_upgrade_preserves_choice_backup_and_context_suffix(self):
        from test_models import VARIANTS
        old_models = [f"claude-opus-5-5-{effort}[1m]" for effort in ("high", "xhigh", "max")]
        with patch.object(switcher, "MODELS", old_models):
            self.switch("cursor")
        original_backup = json.loads(self.state.read_text())["original"]
        for alias, context, effort, fast in VARIANTS:
            settings = self.load()
            settings["model"] = alias
            self.write(settings)
            self.switch("cursor")
            self.assertEqual(self.load()["model"], alias + ("[1m]" if context == "1m" else ""))
            self.assertEqual(self.load()["availableModels"], switcher.MODELS)
            self.assertEqual(json.loads(self.state.read_text())["original"], original_backup)
        self.assertEqual(len(self.probes), 1)
        self.switch("restore")
        self.assertEqual(self.load(), ORIGINAL)

    def test_cursor_installs_local_max_and_restore_keeps_unrelated_edits(self):
        self.assertEqual(self.switch("cursor"), "switched")
        settings = self.load()
        env = settings["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]),
                         ("http://127.0.0.1:8790", switcher.LOCAL_TOKEN))
        self.assertEqual([env[name] for name in switcher.MODEL_KEYS] + [settings["model"]],
                         [switcher.MAIN] * 3 + [switcher.FAST, switcher.MAIN, switcher.MAIN])
        self.assertEqual(settings["availableModels"], switcher.MODELS)
        self.assertEqual(env["NO_PROXY"], "relay.example,.internal,127.0.0.1,localhost")
        self.assertEqual(env["no_proxy"], env["NO_PROXY"])
        self.assertEqual((env["API_TIMEOUT_MS"], env["CLAUDE_CODE_MAX_RETRIES"]),
                         (switcher.API_TIMEOUT_MS, switcher.MAX_RETRIES))
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"], "1")
        self.assertEqual(env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"], switcher.STREAM_IDLE_TIMEOUT_MS)
        self.assertEqual(env["CLAUDE_CODE_EFFORT_LEVEL"], "max")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(settings["hooks"], ORIGINAL["hooks"])
        self.assertEqual(self.probes, [(8790, "high")])
        for path, mode in ((self.settings, 0o600), (self.state, 0o600), (self.state.parent, 0o700)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.write({**settings, "theme": "light"})
        self.assertEqual(self.switch("restore"), "restored")
        restored = self.load()
        self.assertEqual(restored, {**ORIGINAL, "theme": "light"})
        self.assertEqual((list(restored), list(restored["env"])), (list(ORIGINAL), list(ORIGINAL["env"])))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.switch("restore"), "unchanged")

    def test_existing_api_key_is_replaced_and_restored(self):
        self.write({**ORIGINAL, "env": {**ORIGINAL["env"], "ANTHROPIC_API_KEY": SECRET}})
        self.switch("cursor")
        self.assertEqual(self.load()["env"]["ANTHROPIC_API_KEY"], switcher.LOCAL_TOKEN)
        self.switch("restore")
        self.assertEqual(self.load()["env"]["ANTHROPIC_API_KEY"], SECRET)

    def test_repeated_cursor_is_idempotent_and_skips_the_live_probe(self):
        self.switch("cursor")
        before = self.settings.read_bytes()
        self.assertEqual(self.switch("cursor"), "unchanged")
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(len(self.probes), 1)

    def test_external_provider_edit_is_a_conflict_and_nothing_is_written(self):
        self.switch("cursor")
        edited = self.load()
        edited["env"]["ANTHROPIC_MODEL"] = "someone-else"
        self.write(edited)
        before = self.settings.read_bytes()
        for mode in ("restore", "cursor"):
            with self.assertRaises(switcher.SwitchConflict):
                self.switch(mode)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertTrue(self.state.exists())
        self.assertEqual(switcher.status(self.settings, self.state)["provider"], "conflict")

    def test_failed_live_probe_leaves_settings_untouched(self):
        before = self.settings.read_bytes()
        with patch.object(switcher, "probe_messages", side_effect=ProbeFailed("probe failed")):
            with self.assertRaises(ProbeFailed):
                self.switch("cursor")
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_settings_edited_during_the_probe_abort_the_switch(self):
        def edit(port, effort):
            self.write({**ORIGINAL, "theme": "light"})
        with patch.object(switcher, "probe_messages", side_effect=edit):
            with self.assertRaises(switcher.SwitchConflict):
                self.switch("cursor")
        self.assertEqual(self.load(), {**ORIGINAL, "theme": "light"})
        self.assertFalse(self.state.exists())

    def test_backup_saved_but_settings_unwritten_is_recovered(self):
        real = switcher.atomic_write

        def fail_settings(path, data, mode):
            if path == self.settings:
                raise OSError("disk full")
            real(path, data, mode)
        with patch.object(switcher, "atomic_write", side_effect=fail_settings):
            with self.assertRaises(OSError):
                self.switch("cursor")
        self.assertTrue(self.state.exists())
        self.assertEqual(switcher.status(self.settings, self.state)["provider"], "original")
        self.assertEqual(self.switch("restore"), "unchanged")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.switch("cursor"), "switched")

    def test_status_never_reports_credentials(self):
        self.switch("cursor")
        report = switcher.status(self.settings, self.state)
        self.assertEqual((report["provider"], report["model"]), ("cursor", switcher.MAIN))
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(SECRET, self.settings.read_text())
        self.assertIn(SECRET, self.state.read_text())

    def test_symlinked_settings_are_refused(self):
        target = self.settings.with_name("real.json")
        self.settings.rename(target)
        self.settings.symlink_to(target)
        with self.assertRaises(ValueError):
            self.switch("cursor")
        self.assertFalse(self.state.exists())

    def install_previous_version(self):
        bare = switcher.SERVICE_MODELS
        with patch.multiple(switcher, MODELS=list(bare), MAIN=bare[2], FAST=bare[0]):
            self.assertEqual(self.switch("cursor"), "switched")
        self.assertEqual(self.load()["model"], "claude-opus-5-5-max")

    def test_models_carry_the_1m_suffix_but_the_service_check_uses_bare_ids(self):
        self.assertEqual((switcher.MAIN, switcher.FAST), ("claude-opus-5-5-max[1m]", "claude-opus-5-5-high[1m]"))
        health = {"service": "cursor-sdk2api", "capabilities": ["anthropic_messages"]}

        def serving(ids):
            return lambda port, path: health if path == "/health" else {"data": [{"id": model} for model in ids]}
        with patch.object(switcher, "fetch", side_effect=serving(switcher.SERVICE_MODELS)):
            REAL_VERIFY(8790)
        with patch.object(switcher, "fetch", side_effect=serving(switcher.MODELS)):
            with self.assertRaises(switcher.ServiceNotReady):
                REAL_VERIFY(8790)

    def test_cursor_upgrades_an_earlier_switch_without_probing(self):
        self.install_previous_version()
        self.assertTrue(switcher.status(self.settings, self.state)["update_available"])
        self.assertEqual(self.switch("cursor"), "updated")
        settings = self.load()
        self.assertEqual([settings["env"][name] for name in switcher.MODEL_KEYS] + [settings["model"]],
                         [switcher.MAIN] * 3 + [switcher.FAST, switcher.MAIN, switcher.MAIN])
        self.assertEqual(settings["availableModels"], switcher.MODELS)
        self.assertEqual(len(self.probes), 1)
        report = switcher.status(self.settings, self.state)
        self.assertEqual((report["provider"], report["update_available"]), ("cursor", False))
        self.assertEqual(self.switch("cursor"), "unchanged")
        self.assertEqual(self.switch("restore"), "restored")
        self.assertEqual(self.load(), ORIGINAL)
        self.assertFalse(self.state.exists())

    def test_interrupted_upgrade_is_still_recognized_and_restorable(self):
        self.install_previous_version()
        real = switcher.atomic_write

        def fail_settings(path, data, mode):
            if path == self.settings:
                raise OSError("disk full")
            real(path, data, mode)
        with patch.object(switcher, "atomic_write", side_effect=fail_settings):
            with self.assertRaises(OSError):
                self.switch("cursor")
        self.assertEqual(self.load()["model"], "claude-opus-5-5-max")
        self.assertEqual(switcher.status(self.settings, self.state)["provider"], "cursor")
        self.assertEqual(self.switch("cursor"), "updated")
        self.assertEqual(self.load()["model"], switcher.MAIN)
        self.assertEqual(self.switch("restore"), "restored")
        self.assertEqual(self.load(), ORIGINAL)

    def test_client_timeouts_outlast_the_claude_service_bound(self):
        max_s, queue_s = switcher.MAX_DEADLINE_S, switcher.QUEUE_TIMEOUT_S
        self.assertGreater(int(switcher.STREAM_IDLE_TIMEOUT_MS), (queue_s + max_s + 30) * 1000)
        self.assertGreater(int(switcher.API_TIMEOUT_MS), int(switcher.STREAM_IDLE_TIMEOUT_MS))

    def test_status_asks_for_a_restart_until_service_limits_match(self):
        current = {"deadlines": {"high": 1200.0, "xhigh": 1200.0, "max": 1800.0}, "queue_timeout": 1800.0}
        old = {"deadlines": {"high": 1200.0, "xhigh": 1200.0, "max": 1200.0}, "queue_timeout": 1200.0}
        for limits, restart in ((None, True), (old, True), (current, False)):
            health = {"service": "cursor-sdk2api", "status": "ready", "capabilities": ["anthropic_messages"]}
            if limits is not None:
                health["limits"] = limits
            with self.subTest(limits=limits), patch.object(switcher, "fetch", return_value=health):
                report = REAL_REPORT(8790)
            self.assertEqual("next_step" in report, restart)
            self.assertEqual(report.get("limits"), limits)

    def test_tier_picked_with_slash_model_is_kept_not_a_conflict(self):
        self.switch("cursor")
        settings = self.load()
        settings["model"] = "claude-opus-5-5-xhigh[1m]"
        self.write(settings)
        report = switcher.status(self.settings, self.state)
        self.assertEqual((report["provider"], report["update_available"]), ("cursor", False))
        self.assertEqual(self.switch("cursor"), "unchanged")
        self.assertEqual(self.load()["model"], "claude-opus-5-5-xhigh[1m]")
        del settings["model"]
        self.write(settings)
        self.assertEqual(switcher.status(self.settings, self.state)["provider"], "cursor")
        self.assertEqual(self.switch("restore"), "restored")
        self.assertEqual(self.load(), ORIGINAL)

    def test_upgrade_keeps_the_tier_picked_with_slash_model(self):
        self.install_previous_version()
        settings = self.load()
        settings["model"] = "claude-opus-5-5-xhigh"
        self.write(settings)
        self.assertEqual(self.switch("cursor"), "updated")
        settings = self.load()
        self.assertEqual((settings["model"], settings["env"]["ANTHROPIC_MODEL"]),
                         ("claude-opus-5-5-xhigh[1m]", switcher.MAIN))
        self.assertEqual(self.switch("restore"), "restored")
        self.assertEqual(self.load(), ORIGINAL)

    def test_a_non_cursor_model_is_still_a_conflict(self):
        self.switch("cursor")
        settings = self.load()
        settings["model"] = "relay/other"
        self.write(settings)
        before = self.settings.read_bytes()
        for mode in ("restore", "cursor"):
            with self.assertRaises(switcher.SwitchConflict):
                self.switch(mode)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(switcher.status(self.settings, self.state)["provider"], "conflict")


if __name__ == "__main__":
    unittest.main()
