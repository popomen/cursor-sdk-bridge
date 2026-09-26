#!/usr/bin/env python3
"""Exercise the real Desktop app-server protocol without switching user config.

The default stub run is offline at the model boundary. --live explicitly performs
exactly two Cursor SDK generations. Only the synthetic probe.ping call can leave
the adapter; generated calls to real host tools are rejected before SSE output.
Raw requests, SDK prompts, credentials and app-server stderr are never printed or
saved by this harness. SDK's own history-storage behavior still applies to --live.
"""
import argparse
from collections import Counter, deque
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from cursor_bridge.cursor_sdk2api import Service, make_server
from cursor_bridge.responses_protocol import request_payload, unwrap_fence
from cursor_bridge.tool_output import parse_output, strict_json

ROOT = Path(__file__).resolve().parent
FINAL_TEXT = "DESKTOP_NAMESPACE_PROBE_OK"
PROVIDER = "cursor_desktop_probe"
TOOL_NAME = "probe.ping"


class ProbeFailure(ValueError):
    """A fixed diagnostic code, never raw upstream text."""


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + toml(item) for key, item in value.items()) + "}"
    return json.dumps(value)


def body_shape(body):
    known_tools = {"function", "namespace", "custom", "web_search"}
    known_items = {"message", "reasoning", "function_call", "function_call_output"}
    return {
        "tool_types": dict(Counter(item.get("type") if item.get("type") in known_tools else "other"
                                   for item in body.get("tools", []) if isinstance(item, dict))),
        "input_types": dict(Counter(item.get("type", "message") if item.get("type", "message") in known_items else "other"
                                    for item in body.get("input", []) if isinstance(item, dict))),
    }


class StubSDK:
    def __init__(self):
        self.calls = 0

    async def generate(self, model, prompt):
        self.calls += 1
        if self.calls == 1:
            output = {"type": "function_call", "call_id": "synthetic_probe_call", "name": TOOL_NAME,
                      "arguments": "{}"}
        elif self.calls == 2:
            output = {"type": "message", "text": FINAL_TEXT}
        else:
            raise ProbeFailure("unexpected_extra_sdk_generation")
        return json.dumps({"output": [output]})

    async def close(self):
        pass


class GuardedSDK:
    """Check every model item before Service can expose it to the real host."""
    def __init__(self, backend, model, marker):
        self.backend, self.model, self.marker = backend, model, marker
        self.calls = 0
        self.call_id = None
        self.failure = None
        self.normalized_roundtrip = False

    async def generate(self, model, prompt):
        try:
            require(model == self.model, "sdk_model_alias_mismatch")
            self.calls += 1
            require(self.calls <= 2, "unexpected_extra_sdk_generation")
            payload = request_payload(prompt)
            require(any(tool.get("name") == TOOL_NAME for tool in payload["tools"]), "missing_normalized_probe_tool")
            if self.calls == 2:
                calls = [item for item in payload["input"] if item.get("type") == "function_call"]
                outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
                require(len(calls) == len(outputs) == 1, "normalized_roundtrip_item_count")
                require(calls[0].get("name") == TOOL_NAME and "namespace" not in calls[0], "normalized_namespace_mismatch")
                require(calls[0].get("call_id") == outputs[0].get("call_id") == self.call_id, "normalized_call_id_mismatch")
                require(self.marker in json.dumps(outputs[0].get("output")), "synthetic_tool_result_missing")
                self.normalized_roundtrip = True
            text = await self.backend.generate(model, prompt)
            items, _ = parse_output(unwrap_fence(text), payload["tools"])
            require(len(items) == 1, "unexpected_model_item_count")
            item = items[0]
            if self.calls == 1:
                require(item.get("type") == "function_call" and item.get("name") == TOOL_NAME,
                        "non_probe_tool_or_missing_call_blocked")
                require(strict_json(item.get("arguments", "null")) == {}, "probe_arguments_mismatch")
                self.call_id = item["call_id"]
            else:
                require(item.get("type") == "message" and item.get("text") == FINAL_TEXT,
                        "unexpected_final_model_output_blocked")
            return text
        except ProbeFailure as exc:
            self.failure = str(exc)
            raise
        except Exception as exc:
            self.failure = "sdk_or_output_failure:" + type(exc).__name__
            raise

    async def close(self):
        await self.backend.close()


class ProbeService(Service):
    def __init__(self, backend, timeout):
        super().__init__(backend, timeout)
        self.shapes = []
        self.namespace_roundtrip = False
        self.preparation_error = None

    def prepare(self, body):
        try:
            require(len(self.shapes) < 2, "unexpected_extra_http_generation")
            self.shapes.append(body_shape(body))
            if len(self.shapes) == 2:
                calls = [item for item in body.get("input", []) if item.get("type") == "function_call"]
                outputs = [item for item in body.get("input", []) if item.get("type") == "function_call_output"]
                require(len(calls) == len(outputs) == 1, "host_roundtrip_item_count")
                require(calls[0].get("namespace") == "probe" and calls[0].get("name") == "ping", "host_namespace_mismatch")
                require(calls[0].get("call_id") == outputs[0].get("call_id") == self.backend.call_id,
                        "host_call_id_mismatch")
                self.namespace_roundtrip = True
            return super().prepare(body)
        except Exception as exc:
            self.preparation_error = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
            raise


class AppServer:
    def __init__(self, command, settings):
        args = [command]
        for name, value in settings.items():
            args.extend(["-c", name + "=" + toml(value)])
        args.extend(["app-server", "--listen", "stdio://"])
        environment = dict(os.environ)
        environment["RUST_LOG"] = "off"
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, env=environment,
                                        start_new_session=True)
        self.queue, self.pending = queue.Queue(), deque()
        self.sequence = 0
        self.stderr_lines = 0
        self.reader_error = False
        def read_stdout():
            for line in self.process.stdout:
                try:
                    self.queue.put(json.loads(line))
                except ValueError:
                    self.reader_error = True
            self.queue.put(None)
        def discard_stderr():
            for _ in self.process.stderr:
                self.stderr_lines += 1
        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=discard_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "appserver_deadline_expired")
        try:
            value = self.queue.get(timeout=remaining)
        except queue.Empty:
            raise ProbeFailure("appserver_deadline_expired") from None
        require(value is not None, "appserver_exited_before_completion")
        return value

    def call(self, method, params, timeout=45):
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            value = self.receive(deadline)
            if value.get("id") == request_id and "method" not in value:
                require("error" not in value, "appserver_rpc_rejected:" + method)
                return value.get("result", {})
            self.pending.append(value)

    def next_event(self, deadline):
        return self.pending.popleft() if self.pending else self.receive(deadline)

    def close(self):
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=3)
        # This private child has a new process group; never target the live daemon.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


def auth_digest():
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    return hashlib.sha256(path.read_bytes()).digest() if path.is_file() else None


def run(args):
    model = "claude-opus-5-5-" + args.effort
    report = {"mode": "live" if args.live else "stub", "model": model, "passed": False}
    original_auth = auth_digest()
    app = server = service = None
    server_thread = None
    marker = "SYNTHETIC_PROBE_OUTPUT_" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="cursor-desktop-probe-") as temporary:
        work = Path(temporary)
        try:
            if args.live:
                from cursor_bridge.sdk_backend import SDKBackend
                backend = SDKBackend(args.key_file, work / "sdk-workspace", args.timeout)
            else:
                backend = StubSDK()
            guarded = GuardedSDK(backend, model, marker)
            service = ProbeService(guarded, args.timeout + 30)
            server = make_server(service, 0)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            settings = {
                "model_provider": PROVIDER, "model": model, "model_reasoning_effort": args.effort,
                "model_catalog_json": str(ROOT / "assets/models.json"),
                "features.code_mode": False, "features.code_mode_only": False,
                "features.code_mode_host": True, "features.enable_request_compression": False,
                "features.shell_snapshot": False, "web_search": "disabled", "analytics.enabled": False,
                "history.persistence": "none", "sqlite_home": str(work / "sqlite"), "log_dir": str(work / "logs"),
                "model_providers." + PROVIDER: {"name": "Isolated Desktop probe", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "wire_api": "responses", "requires_openai_auth": False, "supports_websockets": False,
                    "request_max_retries": 0, "stream_max_retries": 0},
            }
            app = AppServer(args.codex, settings)
            app.call("initialize", {"clientInfo": {"name": "codex_desktop", "version": "probe"},
                                    "capabilities": {"experimentalApi": True}})
            app.send({"jsonrpc": "2.0", "method": "initialized"})
            account = app.call("account/read", {"refreshToken": False})
            report["account_type"] = (account.get("account") or {}).get("type")
            report["requires_openai_auth"] = account.get("requiresOpenaiAuth")
            require(account.get("requiresOpenaiAuth") is False, "provider_requires_openai_auth")
            models = app.call("model/list", {"includeHidden": False})
            expected = {"claude-opus-5-5-" + effort for effort in ("high", "xhigh", "max")}
            require({item.get("model") for item in models.get("data", [])} == expected, "model_catalog_mismatch")
            report["model_catalog_verified"] = True
            thread = app.call("thread/start", {"model": model, "modelProvider": PROVIDER, "cwd": str(work),
                "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                "dynamicTools": [{"type": "namespace", "name": "probe", "description": "Synthetic verification only",
                    "tools": [{"type": "function", "name": "ping", "description": "Return a synthetic marker without side effects",
                               "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}]})
            require(thread.get("modelProvider") == PROVIDER and thread["thread"].get("ephemeral") is True,
                    "unexpected_thread_configuration")
            thread_id = thread["thread"]["id"]
            app.call("turn/start", {"threadId": thread_id, "effort": args.effort, "input": [{"type": "text", "text":
                "Call only probe.ping with an empty object exactly once. Do not call any other tool. "
                "After receiving its synthetic result, respond with exactly " + FINAL_TEXT + "."}]})
            deadline = time.monotonic() + 2 * (args.timeout + 30) + 30
            dynamic_calls, final_seen = 0, False
            while True:
                event = app.next_event(deadline)
                if event.get("method") == "item/tool/call":
                    params = event.get("params", {})
                    require(params.get("threadId") == thread_id and params.get("namespace") == "probe"
                            and params.get("tool") == "ping" and params.get("arguments") == {}
                            and params.get("callId") == guarded.call_id and dynamic_calls == 0,
                            "unexpected_host_tool_request_blocked")
                    dynamic_calls += 1
                    app.send({"jsonrpc": "2.0", "id": event["id"], "result": {
                        "contentItems": [{"type": "inputText", "text": marker}], "success": True}})
                elif event.get("method") == "item/completed":
                    item = event.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage":
                        final_seen |= item.get("text") == FINAL_TEXT
                elif event.get("method") == "turn/completed":
                    report["turn_status"] = event.get("params", {}).get("turn", {}).get("status")
                    require(report["turn_status"] == "completed", "turn_failed")
                    break
                elif "method" in event and "id" in event:
                    raise ProbeFailure("unexpected_host_request_blocked")
            require(guarded.calls == 2 and dynamic_calls == 1 and len(service.shapes) == 2, "roundtrip_count_mismatch")
            require(service.namespace_roundtrip and guarded.normalized_roundtrip, "roundtrip_not_verified")
            require(final_seen, "final_message_missing")
            require(not app.reader_error, "invalid_appserver_protocol_output")
            report.update(passed=True, sdk_generations=guarded.calls, synthetic_tool_calls=dynamic_calls,
                          namespace_and_call_id_verified=True, final_message_verified=True)
        except Exception as exc:
            report["failure"] = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
        finally:
            if app:
                try:
                    app.close()
                except Exception as exc:
                    report["cleanup_failure"] = type(exc).__name__
                    report["passed"] = False
                report["appserver_stderr_lines_discarded"] = app.stderr_lines
            if server:
                server.shutdown()
                server.server_close()
            if server_thread:
                server_thread.join(timeout=5)
            if service:
                report["request_shapes"] = service.shapes
                if service.preparation_error:
                    report["preparation_failure"] = service.preparation_error
                if service.backend.failure:
                    report["backend_failure"] = service.backend.failure
                try:
                    service.close()
                except Exception as exc:
                    report["cleanup_failure"] = type(exc).__name__
                    report["passed"] = False
    report["auth_bytes_unchanged"] = original_auth == auth_digest()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stub", action="store_true", help="Use a stub SDK (default)")
    mode.add_argument("--live", action="store_true", help="Run exactly two real Cursor SDK generations")
    parser.add_argument("--effort", choices=("high", "xhigh", "max"), default="high")
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".codex/cursor-sdk-api-key")
    parser.add_argument("--timeout", type=float, default=180, help="Deadline per SDK generation, seconds")
    parser.add_argument("--codex", default="codex", help="Codex executable")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    os.umask(0o077)
    try:
        report = run(args)
    except Exception as exc:
        report = {"passed": False, "failure": type(exc).__name__}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
