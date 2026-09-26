"""SDK bridge launch policy, model identity and workspace isolation helpers."""
import hashlib
import os
from pathlib import Path
import shutil

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
NATIVE_TOOL_EVENTS = {"toolCall", "tool-call-started", "tool-call-completed", "partial-tool-call"}


def model_identity(selection):
    return selection["id"], {p["id"]: p["value"] for p in selection.get("params", [])}


def snapshot(workspace):
    result = {}
    for path in sorted(workspace.rglob("*")):
        name = str(path.relative_to(workspace))
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[name] = ("directory",)
    return result


def proxychains_binary():
    # systemd's PATH may resolve a different binary/config than the shell.
    override = os.environ.get("CURSOR_FALLBACK_PROXYCHAINS")
    local = Path.home() / ".local/bin/proxychains4"
    selected = override or (str(local) if local.is_file() else shutil.which("proxychains4"))
    if not selected or not Path(selected).is_absolute() or not os.access(selected, os.X_OK):
        raise RuntimeError("Set CURSOR_FALLBACK_PROXYCHAINS to an executable absolute path")
    return selected


def bridge_command(route, transport):
    from cursor_sdk._vendor import resolve_bridge_path

    bundle = Path(resolve_bridge_path()).parent.parent
    # Own Node directly, so forced cleanup also terminates the actual bridge.
    command = [str(bundle / "bin" / "node")]
    if transport == "http1":
        command += ["--import", str(Path(__file__).with_name("sdk_http1.mjs"))]
    command += [str(bundle / "dist" / "bin" / "cursor-sdk-bridge.js")]
    if route != "ambient":
        command = ["env"] + [part for name in PROXY_VARS for part in ("-u", name)] + (
            [proxychains_binary(), "-q"] if route == "proxychains" else []) + command
    return command
