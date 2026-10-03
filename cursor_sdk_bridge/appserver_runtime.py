"""Linux Desktop SSH consumer inspection and precisely scoped restart.

Never signals by process name. A restart requires no Desktop proxies, no active
tasks, a verified Unix peer, and a pidfd for that exact process.
"""
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import time

from cursor_sdk_bridge.models import MODELS

ALIASES = set(MODELS)
CONFIG_KEYS = ("model_provider", "model", "model_reasoning_effort", "model_catalog_json")
STALE_CATALOG = ("The running app-server still uses the model catalog it loaded at startup. Disconnect the "
                 "Desktop SSH remote, then run cursor-sdk-bridge switch codex cursor --restart-daemon from a separate SSH terminal.")


def catalog_modalities(path):
    """Input modalities per model in a catalog file; None when it cannot be read."""
    try:
        return {model["slug"]: sorted(model.get("input_modalities") or [])
                for model in json.loads(Path(path).read_text())["models"]}
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        return None


class RuntimeBlocked(RuntimeError):
    """Only fixed, non-secret operator messages may be used here."""


class RPC:
    def __init__(self, path, timeout=8):
        from websockets.sync.client import unix_connect
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeBlocked("Control socket must be private and owned by the current user.")
        self.timeout, self.sequence = timeout, 0
        self.ws = unix_connect(str(path), uri='ws://localhost/', open_timeout=timeout, close_timeout=1,
                               max_size=8 * 1024 * 1024, proxy=None, compression=None, user_agent_header=None)
        self.pid, uid, _ = struct.unpack("3i", self.ws.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            self.ws.close()
            raise RuntimeBlocked("Unexpected control socket owner.")
        try:
            self.call("initialize", {"clientInfo": {"name": "cursor_fallback_doctor", "version": "2"},
                                     "capabilities": {"experimentalApi": True}})
            self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        except BaseException:
            self.ws.close()
            raise

    def call(self, method, params):
        self.sequence += 1
        self.ws.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        end = time.monotonic() + self.timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise RuntimeBlocked("App-server RPC deadline expired.")
            response = json.loads(self.ws.recv(timeout=remaining))
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeBlocked("App-server rejected a required runtime inspection RPC.")
            return response["result"]

    def close(self):
        self.ws.close()


def identity(pid):
    root = Path("/proc") / str(pid)
    if root.stat().st_uid != os.getuid():
        raise RuntimeBlocked("Unexpected daemon user.")
    ticks = int((root / "stat").read_text().rsplit(")", 1)[1].split()[19])
    args = (root / "cmdline").read_bytes().split(b"\0")
    args = [part.decode() for part in args if part]
    executable = str((root / "exe").resolve(strict=True))
    if Path(executable).name != "codex" or "app-server" not in args or "--listen" not in args:
        raise RuntimeBlocked("Socket listener is not the expected native Codex app-server.")
    if not args[args.index("--listen") + 1].startswith("unix://"):
        raise RuntimeBlocked("Only the local Unix app-server listener is supported.")
    return {"pid": pid, "start_ticks": ticks, "executable": executable}


def verify_launch(pid, socket_path):
    args = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
    args = [part.decode() for part in args if part][1:]
    # Desktop's SSH bootstrap uses unix:// with the default control socket.
    # Reject overrides we cannot faithfully recreate; don't print their values.
    prefix = ['-c', 'features.code_mode_host=true', 'app-server', '--listen']
    if args not in (prefix + ['unix://'], prefix + ['unix://' + str(socket_path)]):
        raise RuntimeBlocked('Unrecognized daemon launch arguments; no restart was attempted.')
    if (Path('/proc') / str(pid) / 'cwd').resolve() != Path.home().resolve():
        raise RuntimeBlocked('Unrecognized daemon working directory; no restart was attempted.')


def proxy_pids():
    found = []
    for root in Path("/proc").iterdir():
        if not root.name.isdigit():
            continue
        try:
            if root.stat().st_uid != os.getuid():
                continue
            args = (root / "cmdline").read_bytes().split(b"\0")
            if any(args[i:i + 2] == [b"app-server", b"proxy"] for i in range(len(args) - 1)):
                found.append(int(root.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found


class DesktopRuntime:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.socket_path = self.directory / "app-server-control/app-server-control.sock"

    def mutation_guard(self):
        effective = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        if self.directory.resolve() != effective.resolve():
            raise RuntimeBlocked('Runtime restart only supports the effective Codex home. Use an injected fixture for isolated tests.')

    def inspect(self, tasks=False):
        try:
            rpc = RPC(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            return {"state": "absent"}
        try:
            process = identity(rpc.pid)
            config = rpc.call("config/read", {"includeLayers": False})["config"]
            models, defaults, modalities, cursor = [], [], {}, None
            while True:
                page = rpc.call("model/list", {"includeHidden": True, "limit": 100, "cursor": cursor})
                models.extend(item.get("model", item.get("id")) for item in page["data"])
                defaults.extend(item.get('model', item.get('id')) for item in page['data'] if item.get('isDefault'))
                modalities.update((item.get("model", item.get("id")), sorted(item.get("inputModalities") or []))
                                  for item in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            result = {"state": "running", **process,
                      "config": {key: config.get(key) for key in CONFIG_KEYS}, "models": models,
                      "default_models": defaults, "modalities": modalities}
            result["config"]["model_provider"] = config.get("model_provider") or "openai"
            if tasks:
                cursor, active = None, 0
                while True:
                    page = rpc.call("thread/loaded/list", {"limit": 100, "cursor": cursor})
                    for thread_id in page["data"]:
                        thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                        kind = thread.get("status", {}).get("type")
                        if kind not in ("idle", "notLoaded", "systemError", "active"):
                            raise RuntimeBlocked("Unknown task state; cannot safely restart app-server.")
                        active += kind == "active"
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                result["active_tasks"] = active
            return result
        finally:
            rpc.close()

    @staticmethod
    def matches(snapshot, config):
        if snapshot.get("state") != "running":
            return False
        expected = {key: config.get(key) for key in CONFIG_KEYS}
        expected["model_provider"] = expected["model_provider"] or "openai"
        if snapshot.get("config") != expected:
            return False
        if expected["model_provider"] == "cursor":
            return set(snapshot["models"]) == ALIASES and not DesktopRuntime.catalog_stale(snapshot, config)
        if expected["model_provider"] == "traex":
            from cursor_sdk_bridge.traex import MODELS, DEFAULT_MODEL
            actual = set(snapshot.get('models') or [])
            return (DEFAULT_MODEL in actual and actual.issubset(MODELS)
                    and expected['model'] in actual and bool(expected['model_catalog_json'])
                    and not DesktopRuntime.catalog_stale(snapshot, config))
        selection_ok = expected['model'] in snapshot['models'] if expected['model'] else bool(snapshot.get('default_models'))
        return selection_ok and (
            bool(expected["model_catalog_json"]) or not ALIASES.intersection(snapshot["models"]))

    @staticmethod
    def catalog_stale(snapshot, config):
        """The daemon keeps the catalog it loaded at startup; later file edits need a restart."""
        expected = catalog_modalities(config.get("model_catalog_json"))
        loaded = snapshot.get("modalities")
        return expected is None or loaded is None or any(loaded.get(slug) != value for slug, value in expected.items())

    def idle(self, expected=None):
        self.mutation_guard()
        if proxy_pids():
            raise RuntimeBlocked("Disconnect the Desktop SSH remote, then run this command from a separate SSH terminal.")
        current = self.inspect(tasks=True)
        if current['state'] == 'running':
            verify_launch(current['pid'], self.socket_path)
        if current.get("active_tasks", 0):
            raise RuntimeBlocked("App-server has active tasks. Wait for them to finish before switching.")
        if expected and current.get("state") == "running" and any(
            current.get(key) != expected.get(key) for key in ("pid", "start_ticks")):
            raise RuntimeBlocked("App-server changed during preparation; inspect status before retrying.")
        return current

    def stop(self, expected):
        current = self.idle(expected)
        if current["state"] == "absent":
            return
        pidfd = os.pidfd_open(expected["pid"])
        try:
            if identity(expected["pid"]) != {key: expected[key] for key in ("pid", "start_ticks", "executable")}:
                raise RuntimeBlocked("Daemon identity changed; refusing to signal it.")
            self.idle(expected)
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            if not select.select([pidfd], [], [], 10)[0]:
                # Same pidfd, never a name match or a reused numeric PID.
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                if not select.select([pidfd], [], [], 5)[0]:
                    raise RuntimeBlocked("App-server did not exit within the stop deadline.")
        finally:
            os.close(pidfd)

    def start(self, executable=None):
        self.mutation_guard()
        current = self.idle()
        if current["state"] == "running":
            raise RuntimeBlocked('An unexpected daemon appeared; it was not adopted or stopped.')
        if not executable:
            raise RuntimeBlocked('No verified native daemon executable. Connect Desktop once, then disconnect before switching.')
        with open(executable, 'rb') as binary:
            if binary.read(4) != b'\x7fELF' or Path(executable).name != 'codex':
                raise RuntimeBlocked('Refusing to launch a wrapper instead of the verified native Codex executable.')
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = self.directory / "cursor-fallback-state/daemon.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        environment = dict(os.environ)
        for key in ('NO_PROXY', 'no_proxy'):
            environment[key] = ','.join(filter(None, [environment.get(key), 'localhost', '127.0.0.1', '::1']))
        environment['SSH_AUTH_SOCK'] = str(self.socket_path.parent / 'forwarded-ssh-agent.sock')
        try:
            with os.fdopen(descriptor, "ab") as log:
                child = subprocess.Popen([executable, "-c", "features.code_mode_host=true", "app-server", "--listen",
                                  "unix://" + str(self.socket_path)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True, cwd=Path.home(), env=environment)
        except OSError:
            raise RuntimeBlocked("Failed to start the replacement app-server.") from None
        try:
            end = time.monotonic() + 20
            while time.monotonic() < end:
                if child.poll() is not None:
                    raise RuntimeBlocked('Replacement daemon exited before becoming ready.')
                current = self.inspect()
                if current['state'] == 'running':
                    if current['pid'] != child.pid:
                        raise RuntimeBlocked('A different daemon won the startup race; it was not stopped.')
                    return current
                time.sleep(0.2)
            raise RuntimeBlocked('Replacement app-server did not become ready.')
        except BaseException:
            # Popen owns this child; never kill the process that won a socket race.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            raise


def catalog_reload_required(snapshot, config):
    """Cursor is configured and loaded, but the daemon input modalities predate the catalog file."""
    return (snapshot.get("state") == "running" and config.get("model_provider") == "cursor"
            and (snapshot.get("config") or {}).get("model_provider") == "cursor"
            and set(snapshot.get("models") or []) == ALIASES and DesktopRuntime.catalog_stale(snapshot, config))
