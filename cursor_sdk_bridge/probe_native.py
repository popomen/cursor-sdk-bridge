"""Exercise SDK callbacks across two HTTP turns; live mode consumes Cursor quota.

Only synthetic prompts are used. Receipts contain metadata, never credentials,
tool arguments/results, callback URLs, or model output.
"""
import argparse
import asyncio
from collections import Counter
import contextlib
from datetime import datetime, timezone
import importlib.metadata
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import urllib.request


class PendingProbe:
    """A background run whose callback lifetime is independent of HTTP turns."""

    def __init__(self, driver, timeout=180, expected_calls=1):
        self.driver, self.timeout = driver, timeout
        self.expected_calls = expected_calls
        self.pending = {}
        self.ready = asyncio.Event()
        self.task = None
        self.events = Counter()
        self.metadata = {}
        self.execution_count = 0

    async def execute(self, arguments, context):
        call_id = context.tool_call_id
        if not call_id or call_id in self.pending:
            raise RuntimeError("Missing or duplicate callback identity")
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        self.execution_count += 1
        if len(self.pending) == self.expected_calls:
            self.ready.set()
        return await asyncio.wait_for(future, self.timeout)

    def observe(self, event):
        kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
        if kind:
            self.events[str(kind)] += 1

    async def first(self):
        if self.task is None:
            self.task = asyncio.create_task(self.driver(self))
        ready = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait((ready, self.task), timeout=self.timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
                raise RuntimeError("Run ended without a pending tool callback")
            if ready not in done:
                raise TimeoutError("No callback boundary")
            return {"call_ids": sorted(self.pending), "run_pending": not self.task.done()}
        finally:
            ready.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready

    async def continuation(self, results):
        if set(results) != set(self.pending):
            raise ValueError("Results must exactly match the pending batch")
        if any(future.done() for future in self.pending.values()):
            raise ValueError("Result already delivered")
        for call_id, result in results.items():
            self.pending[call_id].set_result(result)
        metadata = await asyncio.wait_for(asyncio.shield(self.task), self.timeout)
        return {"same_run_completed": self.task.done(), "metadata": metadata}

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
        if self.task:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.task


class ProbeHttpServer:
    """Loopback-only test boundary; it does not expose a production endpoint."""

    def __init__(self, probe):
        loop = asyncio.get_running_loop()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 16384:
                        raise ValueError("Probe request too large")
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if self.path == "/first":
                        work = probe.first()
                    elif self.path == "/results":
                        work = probe.continuation(body["results"])
                    else:
                        raise ValueError("Unknown endpoint")
                    value = asyncio.run_coroutine_threadsafe(work, loop).result(probe.timeout + 5)
                    status = 200
                except Exception as error:
                    # Exception text can contain sensitive upstream details.
                    value, status = {"error_type": type(error).__name__}, 500
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.timeout = probe.timeout

    async def post(self, path, body):
        def request():
            request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout + 10) as response:
                return json.load(response)
        return await asyncio.to_thread(request)

    async def close(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await asyncio.to_thread(self.thread.join, 2)


async def fake_driver(probe):
    """Use the installed Python callback server, with no upstream SDK call."""
    from cursor_sdk import CustomTool, ToolCallbackServer

    callback = ToolCallbackServer(loop=asyncio.get_running_loop())
    callback.register_agent("synthetic-agent", {
        "probe_echo": CustomTool(execute=probe.execute, input_schema={"type": "object"})})

    def invoke(index):
        payload = {"agentId": "synthetic-agent", "toolName": "probe_echo",
                   "toolCallId": "synthetic-call-" + str(index), "args": {"index": index}}
        request = urllib.request.Request(
            callback.endpoint.url + "sdk.v1.SdkCustomToolCallbackService/CallCustomTool",
            data=json.dumps(payload).encode(), headers={
                "Authorization": "Bearer " + callback.endpoint.auth_token,
                "Content-Type": "application/json", "Connect-Protocol-Version": "1"})
        with urllib.request.urlopen(request, timeout=probe.timeout) as response:
            return json.load(response)

    try:
        results = await asyncio.gather(*(asyncio.to_thread(invoke, index)
                                        for index in range(probe.expected_calls)))
        return {"result_content_shape_preserved": all(
            item["result"]["content"][0]["type"] == "text" for item in results)}
    finally:
        await asyncio.to_thread(callback.close)


async def live_driver(probe, key_file, workspace, route, effort):
    from cursor_sdk import AgentOptions, CustomTool, LocalAgentOptions, SendOptions
    from cursor_sdk_bridge.sdk_backend import SDKBackend, USAGE_FIELDS

    backend = SDKBackend(key_file, workspace, timeout=probe.timeout, route=route)
    agent = run = None
    model = {"id": "claude-opus-5-5", "params": [
        {"id": "context", "value": "1m"}, {"id": "effort", "value": effort},
        {"id": "fast", "value": "false"}]}
    try:
        await backend._start()
        # A read-only credential check precedes any billable inference.
        try:
            await backend.client.me(api_key=backend.key)
            probe.metadata["credential_probe"] = "valid"
        except Exception:
            probe.metadata["credential_probe"] = "failed"
            raise
        agent = await backend.client.create_agent(AgentOptions(
            api_key=backend.key, model=model, tools=["mcp"],
            disallowed_tools=["shell", "read", "edit", "task", "webSearch", "webFetch"],
            local=LocalAgentOptions(cwd=str(workspace), setting_sources=[], custom_tools={
                "probe_echo": CustomTool(
                    execute=probe.execute, description="Return a synthetic verification result.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                                  "required": ["value"], "additionalProperties": False})})))
        probe.metadata["inference_started"] = True
        run = await agent.send(
            "Call probe_echo exactly once with value 'synthetic-check'. Wait for its result. "
            "After receiving the result, reply with exactly NATIVE_PROBE_OK. Do not call other tools.",
            SendOptions(on_delta=probe.observe))
        full = done = False
        async for event in run.events():
            probe.events["envelope:" + event.kind] += 1
            full |= event.result_is_full
            done |= event.kind == "done"
        result = await run.wait()
        usage = getattr(result, "usage", None)
        return {"status": result.status, "complete_envelopes": full and done,
                "synthetic_response_matches": result.result.strip() == "NATIVE_PROBE_OK",
                "model": model, "usage": {field: getattr(usage, field, 0) for field in USAGE_FIELDS}}
    finally:
        if run is not None and getattr(run, "status", "") not in ("finished", "error", "cancelled"):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(run.cancel(), 3)
        if agent is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(agent.close(), 3)
        await backend.close()


async def run_probe(*, live=False, key_file=None, route="proxychains", effort="high", timeout=180):
    started = time.monotonic()
    receipt = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "sdk_version": importlib.metadata.version("cursor-sdk"), "live": live,
               "scope": "upstream SDK run" if live else "real SDK callback server with FakeSdk run",
               "upstream_inference_calls": 0}
    with tempfile.TemporaryDirectory(prefix="cursor-sdk-bridge-native-probe-") as directory:
        workspace = Path(directory) / "workspace"
        async def driver(probe):
            if live:
                return await live_driver(probe, key_file, workspace, route, effort)
            return await fake_driver(probe)
        probe = PendingProbe(driver, timeout=timeout, expected_calls=1 if live else 2)
        server = ProbeHttpServer(probe)
        try:
            async with asyncio.timeout(timeout + 15):
                first = await server.post("/first", {})
                # urlopen's response context has closed before the second request.
                receipt["first_http_response_completed"] = True
                receipt["pending_survived_first_http_response"] = (
                    first["run_pending"] and not probe.task.done()
                    and all(not future.done() for future in probe.pending.values()))
                receipt["parallel_pending_count"] = len(probe.pending)
                results = {call_id: {"content": [{"type": "text", "text": "synthetic-approved"}]}
                           for call_id in first["call_ids"]}
                second = await server.post("/results", {"results": results})
                receipt["second_http_response_completed_same_run"] = second["same_run_completed"]
                receipt.update(second["metadata"])
                receipt["success"] = (receipt["pending_survived_first_http_response"]
                    and receipt["second_http_response_completed_same_run"]
                    and (not live or (receipt["status"] == "finished"
                         and receipt["complete_envelopes"] and receipt["synthetic_response_matches"])))
        except Exception as error:
            receipt.update(success=False, error_type=type(error).__name__)
        finally:
            await probe.close()
            await server.close()
        receipt["upstream_inference_calls"] = int(probe.metadata.get("inference_started", False))
        receipt["callback_execution_count"] = probe.execution_count
        receipt["event_types"] = dict(probe.events)
        receipt.update({key: value for key, value in probe.metadata.items() if key != "inference_started"})
    receipt["elapsed_s"] = round(time.monotonic() - started, 3)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use one real, billable SDK run")
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".codex/cursor-sdk-api-key")
    parser.add_argument("--route", choices=("proxychains", "direct", "ambient"), default="proxychains")
    parser.add_argument("--effort", choices=("high", "xhigh", "max"), default="high")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("Output exists; choose a new receipt path")
    receipt = asyncio.run(run_probe(live=args.live, key_file=args.key_file, route=args.route,
                                   effort=args.effort, timeout=args.timeout))
    text = json.dumps(receipt, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(text)
    print(text, end="")
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
