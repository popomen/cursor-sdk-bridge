#!/usr/bin/env python3
"""Recoverable configuration switch; preserve auth and verify the consumer."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import tempfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent
MIN_ADAPTER_VERSION = 3
CATALOG_PATH_UPGRADE = (
    "The Cursor model catalog points at an older installation. Disconnect the Desktop SSH remote, "
    "then run cursor-bridge switch codex cursor --restart-daemon from a separate SSH terminal "
    "to update the managed catalog path and verify the restarted daemon."
)
MANAGED = [(key,) for key in ("model", "model_provider", "model_catalog_json", "model_reasoning_effort",
                             "web_search")]
MANAGED += [("features", "code_mode"), ("features", "code_mode_only"),
            ("features", "enable_request_compression"), ("model_providers", "cursor")]


class ServiceNotReady(ValueError):
    """Only fixed service readiness diagnostics, never an upstream body."""


def read(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("refusing non-regular managed file")
    return path.read_bytes() if path.exists() else None


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic(path, value):
    read(path)
    if value is None:
        path.unlink(missing_ok=True)
        sync_dir(path.parent)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".cursor-switch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def encode(value):
    return base64.b64encode(value).decode() if value is not None else None


def restore_missing_auth(path, value):
    """Publish a fully written legacy credential only if no login exists."""
    descriptor, temporary = tempfile.mkstemp(prefix='.cursor-auth-rescue-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Never replace a concurrently refreshed login.
        except FileExistsError:
            pass
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def decode(value):
    return base64.b64decode(value) if value is not None else None


def get(document, path):
    node = document
    for part in path:
        if part not in node:
            return {"present": False}
        node = node[part]
    return {"present": True, "value": node.unwrap() if hasattr(node, "unwrap") else node}


def managed(document):
    return {".".join(path): get(document, path) for path in MANAGED}


def matches_installed(document, state):
    actual = managed(document)
    # Choosing another advertised effort is normal Cursor usage, not an
    # external-provider conflict. The saved OpenAI baseline remains unchanged.
    effort = document.get("model_reasoning_effort")
    if effort in ("high", "xhigh", "max") and document.get("model") == f"claude-opus-5-5-{effort}":
        for name in ("model", "model_reasoning_effort"):
            actual[name] = state["installed"][name]
    return actual == {name: value for name, value in state["installed"].items() if name in actual}


def assign(document, path, source):
    import tomlkit
    node = document
    for part in path[:-1]:
        if part not in node:
            if not source["present"]:
                return
            node[part] = tomlkit.table()
        if not isinstance(node[part], dict):
            raise ValueError("managed configuration parent must be a table")
        node = node[part]
    if source["present"]:
        node[path[-1]] = source["value"]
    else:
        node.pop(path[-1], None)


def write_state(path, state):
    atomic(path, json.dumps(state, ensure_ascii=False, indent=2).encode())


def retain_cursor_provider(document, baseline, state):
    """Keep the bridge definition available to threads already bound to Cursor."""
    path = ("model_providers", "cursor")
    if get(baseline, path)["present"] or get(document, path)["present"]:
        return False
    source = state["installed"].get("model_providers.cursor", {"present": False})
    if not source["present"]:
        return False
    # The current document can already be the restored baseline after a legacy
    # interruption. The installed snapshot still owns the missing definition.
    assign(document, path, source)
    return True


def finish_transaction(directory, state_path, state, expected_state=None):
    expected_state = read(state_path) if expected_state is None else expected_state
    if read(state_path) != expected_state:
        raise ValueError("interrupted switch state conflicts with external changes")
    transaction = state["transaction"]
    # Multi-file updates are recoverable, not atomic. Check all files before any
    # recovery write; do not overwrite a third party's edits after interruption.
    for name in ("config.toml",):
        current = read(directory / name)
        if current not in (decode(transaction[name]["before"]), decode(transaction[name]["after"])):
            raise ValueError("interrupted switch conflicts with external changes")
    for name in ("config.toml",):
        atomic(directory / name, decode(transaction[name]["after"]))
    if read(state_path) != expected_state:
        raise ValueError("switch state changed externally during transaction completion")
    if transaction["mode"] == "openai":
        atomic(state_path, None)
        return None
    state.pop("transaction")
    write_state(state_path, state)
    return state


def verify_service(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
        health = json.load(response)
        if health.get("service") != "cursor-sdk2api":
            raise ServiceNotReady("Unexpected local service; configuration was not switched.")
        if "namespace_functions" not in health.get("capabilities", []):
            raise ServiceNotReady("Run cursor-bridge restart codex with the repaired adapter before switching.")
        version = health.get("adapter_version")
        if not isinstance(version, int) or version < MIN_ADAPTER_VERSION or "image_inputs" not in health["capabilities"]:
            raise ServiceNotReady("Run cursor-bridge restart codex so it serves the image-capable adapter "
                                  f"(adapter_version {MIN_ADAPTER_VERSION}) before switching.")
    with opener.open(f"http://127.0.0.1:{port}/v1/models", timeout=3) as response:
        actual = {item["id"] for item in json.load(response)["data"]}
    expected = {f"claude-opus-5-5-{effort}" for effort in ("high", "xhigh", "max")}
    if actual != expected:
        raise ServiceNotReady("Service model catalog mismatch; configuration was not switched.")


def service_report(port):
    """Read-only adapter summary for status; /health never carries request data or credentials."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            health = json.load(response)
    except Exception as exc:
        return {"state": "unreachable", "error_type": type(exc).__name__}
    if not isinstance(health, dict) or health.get("service") != "cursor-sdk2api":
        return {"state": "unexpected_service"}
    report = {"state": health.get("status"), "adapter_version": health.get("adapter_version"),
              "image_inputs": "image_inputs" in (health.get("capabilities") or [])}
    if isinstance(health.get("progress"), dict):
        report["progress"] = health["progress"]
    return report


def bundled_catalog():
    catalog = ROOT / "assets/models.json"
    models = json.loads(catalog.read_text())["models"]
    if [m["slug"] for m in models] != [f"claude-opus-5-5-{effort}" for effort in ("high", "xhigh", "max")]:
        raise ValueError("invalid bundled model catalog")
    return catalog


def catalog_path_upgrade_required(config):
    return (config.get("model_provider", "openai") == "cursor"
            and config.get("model_catalog_json") != str(ROOT / "assets/models.json"))


def switch(directory, mode, port=8789, prepared=None):
    import tomlkit
    directory = Path(directory).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("codex directory must be an existing real directory")
    state_dir = directory / "cursor-fallback-state"
    if state_dir.is_symlink():
        raise ValueError("state directory cannot be a symlink")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(state_dir, 0o700)
    lock_path, state_path = state_dir / "switch.lock", state_dir / "state.json"
    read(lock_path)
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_bytes = read(state_path)
        state = json.loads(state_bytes) if state_bytes else None
        if state and state.get("version") not in (1, 2):
            raise ValueError("unsupported switch state version")
        legacy = bool(state and state["version"] == 1)
        if legacy and mode != "openai":
            raise ValueError("legacy state must be recovered with openai first")
        if state and "transaction" in state and not legacy:
            transaction = state["transaction"]
            current = read(directory / "config.toml")
            if current not in (decode(transaction["config.toml"]["before"]),
                               decode(transaction["config.toml"]["after"])):
                raise ValueError("interrupted switch conflicts with external changes")
            upgraded = False
            if transaction["mode"] == "openai":
                baseline = tomlkit.parse((decode(state["original_config"]) or b"").decode())
                restored = tomlkit.parse((decode(transaction["config.toml"]["after"]) or b"").decode())
                if retain_cursor_provider(restored, baseline, state):
                    after = tomlkit.dumps(restored).encode()
                    tomllib.loads(after.decode())
                    # Upgrade only a verified configuration transaction. Runtime
                    # journals recover their original bytes before entering here.
                    state = {**state, "transaction": {"mode": "openai", "config.toml": {
                        "before": encode(current), "after": encode(after)}}}
                    upgraded = True
            staged = json.dumps(state, ensure_ascii=False, indent=2).encode() if upgraded else state_bytes
            if prepared:
                final_state = dict(state)
                transaction = final_state.pop('transaction')
                packed = lambda value: json.dumps(value, ensure_ascii=False, indent=2).encode()
                prepared({'config': decode(transaction['config.toml']['after']),
                          'state': packed(final_state) if transaction['mode'] == 'cursor' else None},
                         staged)
            if read(state_path) != state_bytes:
                raise ValueError("interrupted switch state conflicts with external changes")
            if read(directory / "config.toml") != current:
                raise ValueError("configuration changed during switch preparation")
            if upgraded:
                write_state(state_path, state)
                state_bytes = staged
            state = finish_transaction(directory, state_path, state, expected_state=state_bytes)
            state_bytes = read(state_path)
        config = read(directory / "config.toml")
        document = tomlkit.parse((config or b"").decode())
        provider = document.get("model_provider", "openai")
        if state:
            interrupted_legacy = legacy and "transaction" in state and config in (
                decode(state["transaction"]["config.toml"]["before"]),
                decode(state["transaction"]["config.toml"]["after"]))
            if not matches_installed(document, state) and not interrupted_legacy:
                raise ValueError("managed cursor configuration changed externally")
            if legacy and not interrupted_legacy and get(document, ('cli_auth_credentials_store',)) != state['installed'].get('cli_auth_credentials_store', {'present': False}):
                raise ValueError('legacy credentials store changed externally')
            if mode == "cursor":
                if not catalog_path_upgrade_required(document):
                    return
                verify_service(port)
                catalog = bundled_catalog()
                assign(document, ("model_catalog_json",), {"present": True, "value": str(catalog)})
                # Change only the owned path. Preserve the OpenAI baseline and the user's selected tier.
                state = {**state, "installed": {**state["installed"],
                         "model_catalog_json": get(document, ("model_catalog_json",))}}
                new_config = tomlkit.dumps(document).encode()
            else:
                baseline = tomlkit.parse((decode(state["original_config"]) or b"").decode())
                restore_paths = MANAGED + ([("cli_auth_credentials_store",)] if legacy else [])
                for path in restore_paths:
                    source = get(baseline, path)
                    if source["present"]:
                        value = baseline
                        for part in path:
                            value = value[part]
                        source["value"] = value
                    assign(document, path, source)
                retain_cursor_provider(document, baseline, state)
                # Remove only empty parent tables introduced by this switch.
                for parent in ("features", "model_providers"):
                    if parent not in baseline and parent in document and not document[parent]:
                        del document[parent]
                new_config = tomlkit.dumps(document).encode()
                if legacy:
                    # The old version removed auth. Rescue a missing file only;
                    # never replace a new login or a refreshed credential.
                    if read(directory / "auth.json") is None and state.get("original_auth") is not None:
                        restore_missing_auth(directory / 'auth.json', decode(state['original_auth']))
                    state = {"version": 2, "original_config": state["original_config"],
                             "installed": {k: v for k, v in state["installed"].items() if k != "cli_auth_credentials_store"}}
        else:
            if provider != "openai":
                raise ValueError("no OpenAI baseline for current provider")
            if mode == "openai":
                return
            verify_service(port)
            catalog = bundled_catalog()
            values = {
                "model": "claude-opus-5-5-high", "model_provider": "cursor",
                "model_catalog_json": str(catalog), "model_reasoning_effort": "high",
                "web_search": "disabled",
                "features.code_mode": False, "features.code_mode_only": False,
                "features.enable_request_compression": False,
                "model_providers.cursor": {"name": "Cursor SDK", "base_url": f"http://127.0.0.1:{port}/v1",
                    "wire_api": "responses", "supports_websockets": False, "requires_openai_auth": False},
            }
            for path in MANAGED:
                assign(document, path, {"present": True, "value": values[".".join(path)]})
            state = {"version": 2, "original_config": encode(config),
                     "installed": managed(document)}
            new_config = tomlkit.dumps(document).encode()
        tomllib.loads(new_config.decode())
        if read(directory / "config.toml") != config:
            raise ValueError("configuration changed during switch preparation")
        state["transaction"] = {"mode": mode,
            "config.toml": {"before": encode(config), "after": encode(new_config)}}
        if prepared:
            final_state = dict(state)
            final_state.pop("transaction")
            packed = lambda value: json.dumps(value, ensure_ascii=False, indent=2).encode()
            prepared({"config": new_config,
                      "state": packed(final_state) if mode == "cursor" else None},
                     packed(state))
        if read(directory / "config.toml") != config or read(state_path) != state_bytes:
            raise ValueError("configuration or switch state changed during switch preparation")
        staged = json.dumps(state, ensure_ascii=False, indent=2).encode()
        write_state(state_path, state)
        finish_transaction(directory, state_path, state, expected_state=staged)


def status(directory):
    config = tomllib.loads((read(directory / "config.toml") or b"").decode())
    state = directory / "cursor-fallback-state/state.json"
    pending = json.loads(read(state) or b"{}")
    runtime_journal = directory / "cursor-fallback-state/runtime-transition.json"
    transition = json.loads(read(runtime_journal) or b"{}")
    upgrade = catalog_path_upgrade_required(config)
    report = {"provider": config.get("model_provider", "openai"), "model": config.get("model"),
            "model_catalog_json": config.get("model_catalog_json"),
            "expected_model_catalog_json": str(ROOT / "assets/models.json"),
            "catalog_path_upgrade_required": upgrade,
            "auth_file_present": read(directory / "auth.json") is not None,
            "openai_config_backup_present": bool(pending), "auth_policy": "preserved",
            "transaction_pending": "transaction" in pending or bool(transition),
            "runtime_transition": {key: transition.get(key) for key in ("mode", "phase")} if transition else None,
            "managed_config_conflict": bool(pending and not matches_installed(config, pending))}
    if upgrade:
        report["next_step"] = CATALOG_PATH_UPGRADE
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("openai", "cursor", "status"))
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--restart-daemon", action="store_true",
                        help="verify no Desktop proxy/active tasks, then restart the exact Unix socket owner")
    args = parser.parse_args()
    try:
        if args.command != "status":
            from cursor_bridge.runtime_switch import apply_mode
            apply_mode(args.codex_dir, args.command, args.port, args.restart_daemon)
        result = status(args.codex_dir)
        from cursor_bridge.appserver_runtime import STALE_CATALOG, DesktopRuntime, catalog_reload_required
        runtime = DesktopRuntime(args.codex_dir)
        try:
            observed = runtime.inspect()
            config = tomllib.loads((read(args.codex_dir / "config.toml") or b"").decode())
            result["runtime"] = observed
            result["runtime_matches_config"] = runtime.matches(observed, config)
            if catalog_reload_required(observed, config):
                result["catalog_reload_required"] = True
                result.setdefault("next_step", STALE_CATALOG)
        except Exception as exc:
            result["runtime"] = {"state": "unverified", "error_type": type(exc).__name__}
            result["runtime_matches_config"] = False
        result["service"] = service_report(args.port)
        if result["provider"] == "cursor" and not result["service"].get("image_inputs"):
            result["service"]["next_step"] = ("Run cursor-bridge restart codex when idle so the service "
                                              "uses the image-capable adapter.")
        print(json.dumps(result, ensure_ascii=False))
        if args.command != "status":
            if (not result['runtime_matches_config'] or result['transaction_pending']
                    or (args.command == 'cursor' and result['catalog_path_upgrade_required'])):
                from cursor_bridge.appserver_runtime import RuntimeBlocked
                raise RuntimeBlocked('Final runtime verification changed, catalog upgrade is pending, or a transaction remains; inspect status before reconnecting.')
            print("Remote daemon provider and model list verified. Reconnect Desktop; create a NEW task for the new provider.")
    except Exception as exc:
        from cursor_bridge.appserver_runtime import RuntimeBlocked
        from cursor_bridge.probe_service import ProbeFailed
        # runtime_switch imports this module by name when this file is __main__.
        from cursor_bridge.switch_config import ServiceNotReady as ImportedServiceNotReady
        if isinstance(exc, (RuntimeBlocked, ProbeFailed, ServiceNotReady, ImportedServiceNotReady)):
            print(str(exc))
            raise SystemExit(1)
        # Configuration/parser errors can include secrets; print classes only.
        print("Switch failed safely: " + type(exc).__name__ + ". Check service, managed config/auth conflicts, or pending transaction.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
