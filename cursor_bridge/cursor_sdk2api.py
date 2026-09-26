#!/usr/bin/env python3
"""Loopback-only Responses adapter for text, image input and functions on Cursor SDK 1.0.32."""
import argparse
import asyncio
from collections import OrderedDict
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
from pathlib import Path
import threading
import time

from cursor_bridge.anthropic_protocol import (ERROR_TYPES, complete_message, error_body, estimate_tokens, failure_error,
                                message_shell, prepare_messages, stream_events)
from cursor_bridge.failures import InvalidModelOutput, PromptTooLarge, RequestTimeout, error_code, failure_label
from cursor_bridge.request_log import REQUEST_STATS, RequestLog
from cursor_bridge.ledger import ResultLedger, request_digest
from cursor_bridge.responses_protocol import (InvalidRequest, MODELS, complete_response, completion_events,
                                prepare_request, response_shell, strict_json)

MAX_BODY = 64 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_PROMPT_BYTES = 3 * 1024 * 1024
MAX_TIMEOUT = 1800
REQUEST_MARGIN = 30
KEEPALIVE_SECONDS = 10
POST_PATHS = ("/v1/responses", "/v1/messages", "/v1/messages/count_tokens")
# Each Claude Code retry reruns a full SDK inference; by default it retries a 5xx up to 10 times.
NO_RETRY = (("x-should-retry", "false"),)


def model_list():
    # Codex reads the OpenAI fields; Claude Code gateway model discovery reads the Anthropic ones.
    data = [{"id": model, "object": "model", "created": 0, "owned_by": "cursor", "type": "model",
             "display_name": f"Opus 5.5 {effort} (Cursor)", "created_at": "2026-09-25T00:00:00Z"}
            for model, effort in MODELS.items()]
    return {"object": "list", "data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}


class Service:
    def __init__(self, backend, timeout=None, log=None, max_prompt_bytes=MAX_PROMPT_BYTES,
                 ledger_path=None, ledger_ttl=3600, mode="legacy"):
        self.backend, self.timeout, self.log = backend, timeout, log
        self.max_prompt_bytes = max_prompt_bytes
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.cache, self.cache_lock = OrderedDict(), threading.Lock()
        self.admission = threading.BoundedSemaphore(8)
        self.submit_lock, self.closing = threading.RLock(), False
        self.draining, self.jobs, self.mode = False, {}, mode
        self.ledger = ResultLedger(ledger_path, ttl=ledger_ttl) if ledger_path else None

    def deadline(self, model):
        if self.timeout is not None:
            return self.timeout
        backend_deadline = getattr(self.backend, "deadline", None)
        if backend_deadline is None:
            return 210
        return getattr(self.backend, "queue_timeout", 0) + backend_deadline(model) + REQUEST_MARGIN

    def record(self, **fields):
        if self.log is not None:
            self.log.record(**fields)

    def prepare(self, body):
        previous = None
        if isinstance(body, dict) and body.get("previous_response_id"):
            with self.cache_lock:
                entry = self.cache.get(body["previous_response_id"])
                if entry is None or entry[0] < time.monotonic() - 3600:
                    raise InvalidRequest("previous_response_id expired or unknown; send full input history")
                previous = entry[1]
        history, prompt, images = prepare_request(body, previous)
        return response_shell(body["model"]), history, prompt, images

    async def _generate(self, shell, body, history, prompt, stats=None, images=()):
        REQUEST_STATS.set(stats)
        scope = asyncio.timeout(self.deadline(body["model"]))
        try:
            async with scope:
                # Text-only backends (probes, fixtures) accept no images argument.
                extra = {"images": images} if images else {}
                generate_request = getattr(self.backend, "generate_request", None)
                if generate_request:
                    text = await generate_request(body["model"], prompt, body, history, **extra)
                else:
                    text = await self.backend.generate(body["model"], prompt, **extra)
        except TimeoutError:
            if scope.expired():
                raise RequestTimeout() from None
            raise
        try:
            response = complete_response(shell, text, body, history)
        except Exception:
            raise InvalidModelOutput() from None
        commit = getattr(self.backend, "commit_response", None)
        if commit:
            commit(history, body, response, getattr(text, "reuse_token", None))
        self._remember(body, history, response)
        return response

    def _remember(self, body, history, response):
        if body.get("store", True):
            with self.cache_lock:
                items = history + response["output"]
                self.cache[response["id"]] = (time.monotonic(), items, len(json.dumps(items)))
                # Bound history memory as well as response count. Cache is local,
                # volatile and intentionally excludes stored SDK credentials.
                while len(self.cache) > 32 or sum(v[2] for v in self.cache.values()) > MAX_CACHE_BYTES:
                    self.cache.popitem(last=False)

    async def _run_job(self, digest, shell, body, history, prompt, stats, images):
        began = time.monotonic()
        outcome = "completed"
        try:
            result = await self._generate(shell, body, history, prompt, stats, images)
            if self.ledger:
                self.ledger.put(digest, result, stats)
            return result
        except BaseException as exc:
            outcome = "service_stopped" if isinstance(exc, asyncio.CancelledError) else failure_label(exc)
            raise
        finally:
            if self.ledger:
                self.record(event="inference", model=body["model"], **stats, outcome=outcome,
                            duration_s=round(time.monotonic() - began, 3))

    def lifecycle(self):
        with self.submit_lock:
            pending = getattr(self.backend, "pending_count", lambda: 0)()
            return {"draining": self.draining, "unfinished": len(self.jobs) + pending}

    def submit(self, shell, body, history, prompt, stats=None, images=()):
        stats = stats if stats is not None else {}
        digest = request_digest(body, history, self.mode)
        with self.submit_lock:
            if self.closing or self.draining:
                raise RuntimeError("service is draining")
            if self.ledger:
                existing = self.jobs.get(digest)
                if existing:
                    future, prior_shell, prior_stats = existing
                    shell.update(prior_shell)
                    def joined(done):
                        stats.update(prior_stats, dedup="joined")
                    future.add_done_callback(joined)
                    return future
                saved = self.ledger.get(digest)
                if saved:
                    response = saved["response"]
                    shell.update({key: response[key] for key in ("id", "created_at", "model")})
                    stats.update(saved["stats"], dedup="hit")
                    self._remember(body, history, response)
                    future = concurrent.futures.Future()
                    future.set_result(response)
                    return future
            if len(self.jobs) >= 8:
                raise RuntimeError("inference queue is full")
            # Keep separate jobs in fixtures/explicit no-ledger mode.
            key = digest if self.ledger else shell["id"]
            future = asyncio.run_coroutine_threadsafe(
                self._run_job(digest, shell, body, history, prompt, stats, images), self.loop)
            self.jobs[key] = (future, dict(shell), stats)
            def finished(done):
                with self.submit_lock:
                    self.jobs.pop(key, None)
            future.add_done_callback(finished)
            return future

    async def _close(self):
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        # Cancel requests first; shielded SDK launch tasks must retain their
        # internal deadline and ownership cleanup rather than be cancelled here.
        tasks = [task for task in tasks if task.get_coro().__qualname__.endswith("Service._run_job")]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.backend.close()

    def close(self):
        with self.submit_lock:
            self.closing = True
        future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        try:
            future.result(timeout=45)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()
            if self.ledger:
                self.ledger.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "cursor-sdk2api/3"

    def log_message(self, format, *args):
        pass  # Request bodies, headers and raw SDK exceptions are never logged.

    def _json(self, status, value, headers=()):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, text in headers:
            self.send_header(name, text)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message, code=None, param=None):
        self._json(status, {"error": {"type": "invalid_request_error" if status < 500 else "server_error",
                                     "message": message, "code": code, "param": param}})

    def _anthropic_error(self, status, message, code=None, param=None):
        self._json(status, error_body(ERROR_TYPES.get(status, "api_error"), message + (f" ({param})" if param else "")))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            self._json(200, model_list())
        elif path == "/health":
            backend = self.server.service.backend
            progress, limits = getattr(backend, "progress", None), getattr(backend, "limits", None)
            from cursor_bridge.version import running_version
            self._json(200, {"running_version": running_version(), "mode": self.server.service.mode,
                             **self.server.service.lifecycle(), "service": "cursor-sdk2api", "status": "ready", "adapter_version": 3,
                             "capabilities": ["namespace_functions", "image_inputs", "structured_text_outputs", "safe_error_paths",
                                              "failure_labels", "data_keepalive", "sdk_progress", "anthropic_messages"],
                             "progress": progress() if progress else None,
                             "limits": limits() if limits else None})
        else:
            self._error(404, "unknown endpoint")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        anthropic = path.startswith("/v1/messages")
        fail, api = (self._anthropic_error, {"api": "messages"}) if anthropic else (self._error, {})
        if path in ("/admin/drain", "/admin/resume"):
            if self.headers.get("Origin") or self.headers.get("Transfer-Encoding"):
                return fail(400, "unsupported admin request")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return fail(400, "invalid admin request")
            if not 0 <= length <= 16:
                return fail(400, "invalid admin request")
            self.connection.settimeout(5)
            if length:
                self.rfile.read(length)
            service = self.server.service
            with service.submit_lock:
                service.draining = path == "/admin/drain"
                state = service.lifecycle()
            return self._json(409 if state["draining"] and state["unfinished"] else 200, state)
        if path not in POST_PATHS:
            return fail(404, "unknown endpoint")
        # No browser-origin requests: local tools can use HTTP without CORS.
        if self.headers.get("Origin") or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
            return fail(400, "Origin, chunked and compressed requests are unsupported")
        service = self.server.service
        began = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return fail(413, "request body must be between 1 byte and 64 MiB")
            self.connection.settimeout(15)
            body = strict_json(self.rfile.read(length).decode("utf-8"))
            if anthropic:
                request, history, prompt, images = prepare_messages(body)
            else:
                shell, history, prompt, images = service.prepare(body)
        except InvalidRequest as exc:
            # All messages/paths are adapter-owned constants, never request text
            # or jsonschema exception strings that could contain private values.
            service.record(**api, outcome="invalid_request:" + str(exc.code))
            return fail(400, str(exc), exc.code, exc.param)
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError):
            service.record(**api, outcome="invalid_request:malformed_request")
            return fail(400, "malformed JSON or request shape", "malformed_request")
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": estimate_tokens(len(prompt.encode()), images)})
        streaming = body.get("stream", False)
        entry = {**api, "model": body["model"], "stream": streaming, "input_items": len(history),
                 "prompt_bytes": len(prompt.encode()), "images": len(images),
                 "image_bytes": sum(image["bytes"] for image in images)}
        if not service.admission.acquire(blocking=False):
            service.record(**entry, outcome="queue_full", duration_s=0)
            return fail(429, "request queue is full")
        if anthropic:
            return self._messages(service, body, request, history, prompt, images, entry, began)
        future, started, sequence, outcome, stats = None, False, 0, "completed", {}

        def emit(kind, fields):
            nonlocal sequence
            data = {"type": kind, "sequence_number": sequence, **fields}
            sequence += 1
            self.wfile.write(("event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            if entry["prompt_bytes"] <= service.max_prompt_bytes:
                future = service.submit(shell, body, history, prompt, stats, images)
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                started = True
                emit("response.created", {"response": shell})
                emit("response.in_progress", {"response": shell})
            if entry["prompt_bytes"] > service.max_prompt_bytes:
                raise PromptTooLarge()
            # SSE comments do not reset the Codex stream idle timer; data events do.
            while not concurrent.futures.wait([future], timeout=KEEPALIVE_SECONDS).done:
                if streaming:
                    emit("response.in_progress", {"response": shell})
            result = future.result()
            if streaming:
                for kind, fields in completion_events(result):
                    emit(kind, fields)
            else:
                self._json(200, result)
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client_disconnected"
            # The service owns inference; HTTP disconnect never cancels it.
        except Exception as exc:
            # Labels are adapter-owned constants or class names; exception strings
            # may contain upstream requests, credentials or private prompts.
            outcome = failure_label(exc)
            error = {"code": error_code(outcome), "message": "Cursor SDK response failed (" + outcome + ")"}
            try:
                if started:
                    emit("response.failed", {"response": {**shell, "status": "failed", "error": error}})
                else:
                    self._json(400 if isinstance(exc, PromptTooLarge) else 502, {"error": error})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            service.admission.release()
            service.record(**entry, **stats, outcome=outcome, duration_s=round(time.monotonic() - began, 3),
                           bridge_launches=getattr(service.backend, "launches", None))

    def _messages(self, service, body, request, history, prompt, images, entry, began):
        message, streaming = message_shell(body["model"]), entry["stream"]
        future, started, outcome, stats = None, False, "completed", {}

        def emit(kind, data):
            self.wfile.write(("event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        try:
            # Like the Anthropic API, oversized prompts fail with HTTP 400 before any stream starts.
            if entry["prompt_bytes"] > service.max_prompt_bytes:
                raise PromptTooLarge()
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                started = True
                emit("message_start", {"type": "message_start", "message": message})
            future = service.submit(response_shell(body["model"]), request, history, prompt, stats, images)
            while not concurrent.futures.wait([future], timeout=KEEPALIVE_SECONDS).done:
                if streaming:
                    emit("ping", {"type": "ping"})
            result = complete_message(message, future.result(), stats.get("usage"))
            if streaming:
                for kind, data in stream_events(result):
                    emit(kind, data)
            else:
                self._json(200, result)
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client_disconnected"
            # The service owns inference; HTTP disconnect never cancels it.
        except Exception as exc:
            outcome = failure_label(exc)
            status, error = failure_error(outcome, entry["prompt_bytes"], service.max_prompt_bytes)
            try:
                if started:
                    emit("error", error)
                else:
                    self._json(status, error, NO_RETRY)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            service.admission.release()
            service.record(**entry, **stats, outcome=outcome, duration_s=round(time.monotonic() - began, 3),
                           bridge_launches=getattr(service.backend, "launches", None))


def make_server(service, port=8789):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.service = service
    return server


def main():
    from cursor_bridge.sdk_backend import DEFAULT_QUEUE_TIMEOUT, DEFAULT_TIMEOUTS, SDKBackend

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--mode", choices=("legacy", "reuse", "native"), default="legacy")
    parser.add_argument("--no-ledger", action="store_true", help="disable retry deduplication")
    parser.add_argument("--dedup-ttl", type=float, default=3600)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".codex/cursor-sdk2api")
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".codex/cursor-sdk-api-key")
    parser.add_argument("--timeout", type=float, help="SDK deadline in seconds for every effort")
    for effort, seconds in DEFAULT_TIMEOUTS.items():
        variable = "CURSOR_FALLBACK_TIMEOUT_" + effort.upper()
        parser.add_argument("--timeout-" + effort, type=float, default=float(os.environ.get(variable, seconds)),
                            help=f"SDK deadline for {effort}, seconds (env {variable}, default {seconds})")
    parser.add_argument("--queue-timeout", type=float,
                        default=float(os.environ.get("CURSOR_FALLBACK_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT)),
                        help="Maximum wait behind other SDK requests, seconds "
                             f"(env CURSOR_FALLBACK_QUEUE_TIMEOUT, default {DEFAULT_QUEUE_TIMEOUT})")
    parser.add_argument("--max-prompt-bytes", type=int,
                        default=int(os.environ.get("CURSOR_FALLBACK_MAX_PROMPT_BYTES", MAX_PROMPT_BYTES)),
                        help="Reject larger prompts as context_length_exceeded (env CURSOR_FALLBACK_MAX_PROMPT_BYTES)")
    parser.add_argument("--log-dir", type=Path, help="Private request metadata log directory (default STATE_DIR/logs)")
    parser.add_argument("--route", choices=("proxychains", "ambient", "direct"), default="proxychains")
    args = parser.parse_args()
    timeouts = {effort: args.timeout if args.timeout is not None else getattr(args, "timeout_" + effort)
                for effort in DEFAULT_TIMEOUTS}
    if not all(0 < seconds <= MAX_TIMEOUT for seconds in timeouts.values()):
        parser.error(f"timeouts must be greater than 0 and at most {MAX_TIMEOUT} seconds")
    if not 0 < args.queue_timeout <= MAX_TIMEOUT:
        parser.error(f"--queue-timeout must be greater than 0 and at most {MAX_TIMEOUT} seconds")
    if args.max_prompt_bytes <= 0:
        parser.error("--max-prompt-bytes must be positive")
    os.umask(0o077)
    backend_type = SDKBackend
    if args.mode == "reuse":
        from cursor_bridge.reuse_backend import ReuseSDKBackend
        backend_type = ReuseSDKBackend
    elif args.mode == "native":
        parser.error("native mode is not yet available; use reuse or legacy")
    backend = backend_type(args.key_file, args.state_dir / "workspace", route=args.route, timeouts=timeouts,
                         queue_timeout=args.queue_timeout)
    service = Service(backend, log=RequestLog(args.log_dir or args.state_dir / "logs"),
                      max_prompt_bytes=args.max_prompt_bytes, mode=args.mode,
                      ledger_path=None if args.no_ledger else args.state_dir / "results.sqlite3",
                      ledger_ttl=args.dedup_ttl)
    server = make_server(service, args.port)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print(f"cursor-sdk2api listening on 127.0.0.1:{server.server_port}; deadlines {timeouts}; queue {args.queue_timeout}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
