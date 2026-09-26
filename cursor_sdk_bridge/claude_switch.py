#!/usr/bin/env python3
"""Point Claude Code at the local Cursor Messages adapter, or restore its original provider.

Only the managed keys of settings.json change; hooks and every other setting are preserved. The
backup keeps the original provider credential in a private 0600 state file and is never printed.
"""
import argparse
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import urllib.request

from cursor_sdk_bridge.probe_service import ProbeFailed, probe_messages

SERVICE_MODELS = [f"claude-opus-5-5-{effort}" for effort in ("high", "xhigh", "max")]
# Claude Code plans a 200k window for claude-* names it does not know; [1m] makes it plan for 1M. It strips
# the suffix and adds the 1M context beta header before sending, so the adapter still receives SERVICE_MODELS.
MODELS = [model + "[1m]" for model in SERVICE_MODELS]
MAIN, FAST = MODELS[2], MODELS[0]
DEFAULT_PORT = 8790
LOCAL_TOKEN = "cursor-sdk2api-local"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Must match the CLI's Claude serve defaults; bounds are queue + inference + 30 s.
MAX_DEADLINE_S, QUEUE_TIMEOUT_S = 1800, 1800
REQUEST_BOUND_S = QUEUE_TIMEOUT_S + MAX_DEADLINE_S + 30
API_TIMEOUT_MS = str((REQUEST_BOUND_S + 270) * 1000)
# Claude Code's event watchdog aborted a pinged 700 s stream at 600 s; outlast the adapter's own bound.
STREAM_IDLE_TIMEOUT_MS = str((REQUEST_BOUND_S + 30) * 1000)
MAX_RETRIES = "2"
ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "API_TIMEOUT_MS",
            "CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS", "NO_PROXY", "no_proxy")
MANAGED = [("env", name) for name in ENV_KEYS] + [("model",), ("availableModels",)]
MODEL_KEYS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


class ServiceNotReady(RuntimeError):
    pass


class SwitchConflict(RuntimeError):
    pass


def lookup(settings, path):
    value = settings
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return {"present": False}
        value = value[part]
    return {"present": True, "value": value}


def managed(settings):
    return {".".join(path): lookup(settings, path) for path in MANAGED}


def installed_versions(state):
    return [state["installed"]] + ([state["previous_installed"]] if "previous_installed" in state else [])


def matches(current, recorded):
    # Claude Code's /model writes the chosen tier back to the top-level model; a Cursor tier there, or none,
    # is the user's choice rather than an external provider change.
    if any(current.get(key) != recorded.get(key) for key in set(current) | set(recorded) if key != "model"):
        return False
    model = current.get("model", {"present": False})
    return model == recorded.get("model") or not model["present"] or model["value"] in MODELS + SERVICE_MODELS


def is_installed(current, state):
    return any(matches(current, version) for version in installed_versions(state))


def assign(settings, path, entry):
    parent = settings
    for part in path[:-1]:
        parent = parent.setdefault(part, {})
    if entry["present"]:
        parent[path[-1]] = entry["value"]
    else:
        parent.pop(path[-1], None)


def with_local_hosts(value):
    hosts = [host for host in (value if isinstance(value, str) else "").split(",") if host]
    return ",".join(hosts + [host for host in LOCAL_HOSTS if host not in hosts])


def chosen_model(settings):
    model = settings.get("model")
    if model in SERVICE_MODELS:
        model += "[1m]"
    return model if model in MODELS else MAIN


def cursor_values(settings, port):
    env = settings.get("env", {})
    values = {("env", name): FAST if name == "ANTHROPIC_DEFAULT_HAIKU_MODEL" else MAIN for name in MODEL_KEYS}
    values.update({("env", "ANTHROPIC_BASE_URL"): f"http://127.0.0.1:{port}",
                   ("env", "ANTHROPIC_AUTH_TOKEN"): LOCAL_TOKEN, ("env", "API_TIMEOUT_MS"): API_TIMEOUT_MS,
                   ("env", "CLAUDE_STREAM_IDLE_TIMEOUT_MS"): STREAM_IDLE_TIMEOUT_MS,
                   ("env", "CLAUDE_CODE_MAX_RETRIES"): MAX_RETRIES,
                   # Otherwise a stream error is retried as a non-streaming request: another full inference.
                   ("env", "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"): "1",
                   ("env", "NO_PROXY"): with_local_hosts(env.get("NO_PROXY")),
                   ("env", "no_proxy"): with_local_hosts(env.get("no_proxy")),
                   ("model",): chosen_model(settings), ("availableModels",): list(MODELS)})
    # Replace an API key or legacy fast-model name only where the user already set one.
    if "ANTHROPIC_API_KEY" in env:
        values[("env", "ANTHROPIC_API_KEY")] = LOCAL_TOKEN
    if "ANTHROPIC_SMALL_FAST_MODEL" in env:
        values[("env", "ANTHROPIC_SMALL_FAST_MODEL")] = FAST
    return values


def read_settings(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Claude Code settings must be an existing regular file, not a symlink")
    raw = path.read_bytes()
    settings = json.loads(raw)
    if not isinstance(settings, dict) or not isinstance(settings.get("env", {}), dict):
        raise ValueError("Claude Code settings must be a JSON object with an env object")
    return raw, settings


def dump(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def atomic_write(path, data, mode):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def load_state(path):
    if path.is_symlink():
        raise ValueError("switch state must not be a symlink")
    if not path.exists():
        return None
    state = json.loads(path.read_bytes())
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("unsupported Claude Code switch state")
    return state


def save_state(path, state):
    atomic_write(path, dump(state), 0o600)


@contextlib.contextmanager
def locked(state_path):
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_path.parent, 0o700)
    with open(state_path.with_name(state_path.name + ".lock"), "a+b") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def fetch(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def verify_service(port):
    try:
        health, listing = fetch(port, "/health"), fetch(port, "/v1/models")
    except Exception as exc:
        raise ServiceNotReady(f"No adapter answers on 127.0.0.1:{port} ({type(exc).__name__}); start "
                              "cursor-sdk-bridge-claude.service first. Settings were not changed.") from None
    if (not isinstance(health, dict) or health.get("service") != "cursor-sdk2api"
            or "anthropic_messages" not in (health.get("capabilities") or [])):
        raise ServiceNotReady(f"127.0.0.1:{port} does not serve Anthropic Messages; restart it with the current "
                              "adapter. Settings were not changed.")
    data = listing.get("data", []) if isinstance(listing, dict) else []
    if [item.get("id") for item in data] != SERVICE_MODELS:
        raise ServiceNotReady("Adapter model catalog mismatch; settings were not changed.")


def service_report(port):
    try:
        health = fetch(port, "/health")
    except Exception as exc:
        return {"port": port, "state": "unreachable", "error_type": type(exc).__name__}
    if not isinstance(health, dict) or health.get("service") != "cursor-sdk2api":
        return {"port": port, "state": "unexpected_service"}
    report = {"port": port, "state": health.get("status"),
              "anthropic_messages": "anthropic_messages" in (health.get("capabilities") or [])}
    if isinstance(health.get("progress"), dict):
        report["progress"] = health["progress"]
    limits = health.get("limits") if isinstance(health.get("limits"), dict) else {}
    if limits:
        report["limits"] = limits
    deadlines = limits.get("deadlines") if isinstance(limits.get("deadlines"), dict) else {}
    if deadlines.get("max") != MAX_DEADLINE_S or limits.get("queue_timeout") != QUEUE_TIMEOUT_S:
        report["next_step"] = ("When idle, run cursor-sdk-bridge restart claude so the max "
                               f"inference and queue limits are {MAX_DEADLINE_S} s; see docs/operations.md.")
    return report


def switch(settings_path, state_path, mode, port=DEFAULT_PORT, probe_effort="high"):
    settings_path, state_path = Path(settings_path).expanduser(), Path(state_path).expanduser()
    with locked(state_path):
        raw, settings = read_settings(settings_path)
        current, state = managed(settings), load_state(state_path)
        if state is not None and current == state["original"] and current != state["installed"]:
            # The backup was saved but settings.json was never switched.
            state_path.unlink()
            state = None
        file_mode = stat.S_IMODE(settings_path.stat().st_mode)
        if mode == "restore":
            if state is None:
                return "unchanged"
            if not is_installed(current, state):
                raise SwitchConflict("Claude Code provider settings changed after the Cursor switch; nothing was "
                                     "restored. Reconcile them with the backup by hand.")
            for path in MANAGED:
                assign(settings, path, state["original"][".".join(path)])
            if state.get("env_created") and not settings.get("env"):
                settings.pop("env", None)
            atomic_write(settings_path, dump(settings), file_mode)
            state_path.unlink()
            return "restored"
        if state is not None:
            if not is_installed(current, state) or state.get("port") != port:
                raise SwitchConflict("Claude Code provider settings differ from the recorded Cursor switch; "
                                     "run restore or reconcile them by hand.")
            verify_service(port)
            for path, value in cursor_values(settings, port).items():
                assign(settings, path, {"present": True, "value": value})
            updated = managed(settings)
            if updated == current:
                return "unchanged"
            if updated != state["installed"]:
                save_state(state_path, {**state, "installed": updated, "previous_installed": current})
            atomic_write(settings_path, dump(settings), file_mode)
            return "updated"
        verify_service(port)
        probe_messages(port, probe_effort)
        if settings_path.read_bytes() != raw:
            raise SwitchConflict("Claude Code settings changed during the live check; nothing was switched. Rerun it.")
        env_created = "env" not in settings
        for path, value in cursor_values(settings, port).items():
            assign(settings, path, {"present": True, "value": value})
        save_state(state_path, {"version": 1, "port": port, "env_created": env_created,
                                "original": current, "installed": managed(settings)})
        atomic_write(settings_path, dump(settings), file_mode)
        return "switched"


def status(settings_path, state_path, port=DEFAULT_PORT):
    settings_path, state_path = Path(settings_path).expanduser(), Path(state_path).expanduser()
    _, settings = read_settings(settings_path)
    current, state = managed(settings), load_state(state_path)
    if state is None or current == state["original"]:
        provider = "original"
    elif is_installed(current, state):
        provider = "cursor"
    else:
        provider = "conflict"
    env = settings.get("env", {})
    report = {"provider": provider, "base_url": env.get("ANTHROPIC_BASE_URL"), "model": settings.get("model"),
              "models": {name: env.get(name) for name in MODEL_KEYS}, "backup_present": state is not None,
              "service": service_report(state["port"] if state else port)}
    if provider == "cursor":
        latest = copy.deepcopy(settings)
        for path, value in cursor_values(latest, state["port"]).items():
            assign(latest, path, {"present": True, "value": value})
        report["update_available"] = managed(latest) != current
    return report


def main():
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "cursor", "restore"))
    parser.add_argument("--settings", type=Path, default=config_dir / "settings.json")
    parser.add_argument("--state-file", type=Path, default=codex_dir / "cursor-fallback-state/claude-code.json")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--probe-effort", choices=("high", "xhigh", "max"), default="high",
                        help="effort of the two live SDK outputs checked before switching (default high)")
    args = parser.parse_args()
    try:
        if args.command != "status":
            result = switch(args.settings, args.state_file, args.command, args.port, args.probe_effort)
            print(json.dumps({"result": result}))
        print(json.dumps(status(args.settings, args.state_file, args.port), ensure_ascii=False))
    except (ServiceNotReady, SwitchConflict, ProbeFailed, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1)
    except Exception as exc:
        # Settings and state hold credentials: report the class only.
        print("Claude Code switch failed safely: " + type(exc).__name__ + ". Run status before retrying.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
