"""Recoverable file + consumer transaction; run after disconnecting Desktop."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import tomllib

from cursor_sdk_bridge.appserver_runtime import STALE_CATALOG, DesktopRuntime, RuntimeBlocked, catalog_reload_required
from cursor_sdk_bridge.probe_service import probe
from cursor_sdk_bridge.switch_config import (CATALOG_PATH_UPGRADE, atomic, bundled_catalog, decode, encode, read,
                                        status, switch, verify_service, write_state)


def same_process(left, right):
    return left.get("state") == "running" and all(left.get(k) == right.get(k) for k in ("pid", "start_ticks"))


def known_files(paths, journal):
    versions = [journal["before"]] + journal.get("versions", [])
    return all(any(read(path) == decode(v[name]) for v in versions) for name, path in paths.items())


def rollback(runtime, paths, journal, journal_path, state_dir):
    # Verify files before stopping anything. Permit every durably recorded
    # intermediate version, including a crash after restoring only one file.
    if not known_files(paths, journal):
        raise RuntimeBlocked("External file changes prevent automatic rollback.")
    original = tomllib.loads((decode(journal["before"]["config"]) or b"").decode())
    current = runtime.idle()
    original_files = all(read(p) == decode(journal["before"][n]) for n, p in paths.items())
    if current["state"] == "running":
        if original_files and runtime.matches(current, original):
            # A bootstrap may have already restored the original consumer.
            restored = current
        elif any(same_process(current, owned) for owned in journal.get("owned", [])):
            runtime.stop(current)
            restored = None
        else:
            raise RuntimeBlocked("An external daemon is running; it was not stopped.")
    else:
        restored = None
    journal["phase"] = "rolling_back"
    write_state(journal_path, journal)
    for name, path in paths.items():
        if not known_files(paths, journal):
            raise RuntimeBlocked('External file changes during rollback; files were preserved.')
        target = decode(journal['before'][name])
        if read(path) != target:
            atomic(path, target)
    if restored is None:
        restored = runtime.start(journal["previous_runtime"].get("executable"))
        journal.setdefault("owned", []).append(restored)
        write_state(journal_path, journal)
    if not runtime.matches(restored, original):
        raise RuntimeBlocked("Rollback files restored but runtime verification failed.")
    if any(read(path) != decode(journal['before'][name]) for name, path in paths.items()):
        raise RuntimeBlocked('External file changes before rollback verification finished.')
    write_state(state_dir / "last-switch.json", {"mode": journal["mode"], "completed_at": time.time(),
                "result": "rolled_back", "pid": restored.get("pid")})
    atomic(journal_path, None)
    return restored


def apply_mode(directory, mode, port=None, restart=False, runtime=None):
    if port is None:
        port = 8792 if mode == "traex" else 8789
    from cursor_sdk_bridge import traex
    verify_target = traex.verify_service if mode == "traex" else verify_service
    directory = Path(directory)
    runtime = runtime or DesktopRuntime(directory)
    current_config = tomllib.loads((read(directory / "config.toml") or b"").decode())
    current = runtime.inspect()
    state_dir = directory / "cursor-fallback-state"
    journal_path = state_dir / "runtime-transition.json"
    pending = read(journal_path)
    file_status = status(directory)
    state = json.loads(read(state_dir / "state.json") or b"{}")
    clean = not file_status["transaction_pending"] and not file_status["managed_config_conflict"] and state.get("version", 2) in (2, 3)
    path_upgrade = mode == "cursor" and file_status["catalog_path_upgrade_required"]
    if clean and not path_upgrade and current_config.get("model_provider", "openai") == mode and runtime.matches(current, current_config):
        if mode in ("cursor", "traex"):
            verify_target(port)
        return current
    if not restart:
        if path_upgrade:
            raise RuntimeBlocked(CATALOG_PATH_UPGRADE)
        if clean and mode == "cursor" and catalog_reload_required(current, current_config):
            raise RuntimeBlocked(STALE_CATALOG)
        raise RuntimeBlocked("Runtime switch not applied. Disconnect the Desktop SSH remote and run cursor-sdk-bridge switch codex openai|cursor|traex|restore --restart-daemon from a separate SSH terminal.")
    preliminary = runtime.idle()
    if not pending and preliminary['state'] != 'running':
        raise RuntimeBlocked('No running daemon to identify. Connect Desktop once, then disconnect and retry; configuration was not changed.')
    if not pending and mode in ("cursor", "traex"):
        verify_target(port)  # Detect old/dead adapters BEFORE stopping a healthy daemon.
        if state.get("version", 2) not in (2, 3) or file_status["managed_config_conflict"]:
            raise RuntimeBlocked("Recover the legacy/conflicting configuration before switching to Cursor.")
        if path_upgrade and not state:
            raise RuntimeBlocked("No saved OpenAI baseline for the catalog upgrade; configuration was not changed.")
        if mode == "cursor":
            bundled_catalog()  # Validate release assets before stopping a healthy daemon.
            probe(port)
        else:
            from cursor_sdk_bridge.probe_traex import probe as probe_traex
            probe_traex(port)  # Two real SDK outputs, synthetic tool only; no generation retry.
    if state_dir.is_symlink():
        raise RuntimeBlocked("Switch state directory cannot be a symlink.")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(state_dir, 0o700)
    lock_path = state_dir / "runtime.lock"
    read(lock_path)
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        paths = {"config": directory / "config.toml", "state": state_dir / "state.json"}
        pending = read(journal_path)
        if pending:
            journal = json.loads(pending)
            # Recovery always restores the saved consumer first. Either command
            # can recover; never require re-entering a broken Cursor mode.
            restored = rollback(runtime, paths, journal, journal_path, state_dir)
            original = tomllib.loads((read(paths["config"]) or b"").decode())
            if (original.get("model_provider", "openai") == mode
                    and not (mode == "cursor" and status(directory)["catalog_path_upgrade_required"])):
                return restored
            raise RuntimeBlocked("Interrupted switch rolled back and previous runtime verified. Run the desired switch again.")
        before = runtime.idle()
        if before['state'] != 'running':
            raise RuntimeBlocked('No running daemon to identify. Connect Desktop once, then disconnect and retry; configuration was not changed.')
        journal = {"version": 1, "mode": mode, "phase": "prepared", "started_at": time.time(),
                   "before": {name: encode(read(path)) for name, path in paths.items()},
                   "previous_runtime": before, "versions": [], "owned": []}
        write_state(journal_path, journal)

        def prepared(final, staging_state):
            # Persist both the intermediate config transaction and its final
            # bytes before the first configuration mutation.
            journal["versions"].extend([
                {"config": encode(final["config"]), "state": encode(staging_state)},
                {name: encode(value) for name, value in final.items()}])
            journal["phase"] = "configured_pending"
            write_state(journal_path, journal)

        try:
            if before["state"] == "running":
                runtime.stop(before)
            if runtime.inspect()["state"] != "absent":
                raise RuntimeBlocked("Desktop bootstrap reconnected before the file update. Disconnect it and retry.")
            switch(directory, mode, port, prepared=prepared)
            # Only claim the bytes prepared before mutation, never an arbitrary
            # post-write snapshot that might already contain an external edit.
            final = journal['versions'][-1] if journal['versions'] else journal['before']
            if any(read(path) != decode(final[name]) for name, path in paths.items()):
                raise RuntimeBlocked('Configuration changed externally during switching.')
            journal["phase"] = "configured"
            write_state(journal_path, journal)
            expected_config = tomllib.loads((decode(final['config']) or b'').decode())
            owned = runtime.start(before.get("executable"))
            journal["owned"].append(owned)
            write_state(journal_path, journal)
            if not runtime.matches(owned, expected_config) or same_process(owned, before):
                raise RuntimeBlocked("Replacement daemon provider/model catalog was not verified.")
            if any(read(path) != decode(final[name]) for name, path in paths.items()):
                raise RuntimeBlocked('Configuration changed externally before runtime verification finished.')
            audit = {"mode": mode, "completed_at": time.time(), "result": "runtime_verified",
                     "previous_pid": before.get("pid"), "pid": owned.get("pid"), "start_ticks": owned.get("start_ticks"),
                     "executable": owned.get('executable'),
                     "config_sha256": hashlib.sha256(read(paths["config"]) or b"").hexdigest(),
                     "model_count": len(owned["models"])}
            write_state(state_dir / "last-switch.json", audit)
            atomic(journal_path, None)
            return owned
        except Exception as exc:
            try:
                rollback(runtime, paths, journal, journal_path, state_dir)
            except Exception:
                journal["phase"] = "recovery_required"
                write_state(journal_path, journal)
                raise RuntimeBlocked("Switch failed; recovery journal retained. OpenAI credentials were preserved. Disconnect Desktop and rerun cursor-sdk-bridge switch codex restore --restart-daemon to recover, or inspect status if external changes exist.") from None
            message = str(exc) if isinstance(exc, RuntimeBlocked) else "Switch failed (" + type(exc).__name__ + ")."
            raise RuntimeBlocked(message + " Previous mode was restored and runtime verified.") from None
