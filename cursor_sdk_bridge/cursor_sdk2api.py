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

from cursor_sdk_bridge.anthropic_protocol import (ERROR_TYPES, complete_message, error_body, estimate_tokens, failure_error,
                                message_shell, prepare_messages, stream_events)
from cursor_sdk_bridge.failures import InvalidModelOutput, NativeProtocolError, PromptTooLarge, RequestTimeout, error_code, failure_label
from cursor_sdk_bridge.request_log import REQUEST_STATS, RequestLog
from cursor_sdk_bridge.ledger import ResultLedger, request_digest
from cursor_sdk_bridge.live_output import LiveOutput, MessagesLive, ResponsesLive
from cursor_sdk_bridge.responses_protocol import (InvalidRequest, MODELS, complete_response, completion_events,
                                prepare_request, response_shell, strict_json)

MAX_BODY = 64 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_PROMPT_BYTES = 3 * 1024 * 1024
MAX_TIMEOUT = 1800
MAX_ADMISSION = 8
REQUEST_MARGIN = 30
KEEPALIVE_SECONDS = 10
POST_PATHS = ("/v1/responses", "/v1/messages", "/v1/messages/count_tokens")
# Each Claude Code retry reruns a full SDK inference; by default it retries a 5xx up to 10 times.
NO_RETRY = (("x-should-retry", "false"),)
CONTEXTS = tuple(dict.fromkeys(spec.context for spec in MODELS.values()))


def model_list():
    # Codex reads the OpenAI fields; Claude Code gateway model discovery reads the Anthropic ones.
    data = [{"id": model, "object": "model", "created": 0, "owned_by": "cursor", "type": "model",
             "display_name": f"{spec.display_name} (Cursor)", "created_at": "2026-09-25T00:00:00Z"}
            for model, spec in MODELS.items()]
    return {"object": "list", "data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}


class Service:
    def __init__(self, backend, timeout=None, log=None, max_prompt_bytes=MAX_PROMPT_BYTES,
                 ledger_path=None, ledger_ttl=3600, mode="legacy", context_prompt_bytes=None):
        self.backend, self.timeout, self.log = backend, timeout, log
        self.max_prompt_bytes = max_prompt_bytes
        # Bytes per SDK token depend on the client's history, so each instance sets its own window limits.
        self.context_prompt_bytes = dict(context_prompt_bytes or {})
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.cache, self.cache_lock = OrderedDict(), threading.Lock()
        self.admission = threading.BoundedSemaphore(MAX_ADMISSION)
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

    def context_limit(self, context):
        return min(self.max_prompt_bytes, self.context_prompt_bytes.get(context, self.max_prompt_bytes))

    def prompt_limit(self, model):
        return self.context_limit(MODELS[model].context)

    def abandon(self, history):
        """Free a paused run whose rejected tool continuation the client will not resend."""
        abandon = getattr(self.backend, "abandon_continuation", None)
        if abandon:
            self.loop.call_soon_threadsafe(abandon, history)

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

    async def _generate(self, shell, body, history, prompt, stats=None, images=(), live=None):
        REQUEST_STATS.set(stats)
        scope = asyncio.timeout(self.deadline(body["model"]))
        try:
            async with scope:
                # Text-only backends (probes, fixtures) accept no images argument.
                extra = {"images": images} if images else {}
                generate_request = getattr(self.backend, "generate_request", None)
                if generate_request:
                    text = await generate_request(body["model"], prompt, body, history,
                                                  on_event=live.append if live else None, **extra)
                else:
                    text = await self.backend.generate(body["model"], prompt, **extra)
        except TimeoutError:
            if scope.expired():
                raise RequestTimeout() from None
            raise
        try:
            response = complete_response(shell, text, body, history)
        except Exception:
            discard = getattr(self.backend, "discard_response", None)
            if discard:
                discard(getattr(text, "reuse_token", None))
            if self.mode == "native":
                raise NativeProtocolError() from None
            raise InvalidModelOutput() from None
        if live is not None and self.mode == "native":
            try:
                response = live.finalize(response)
            except Exception:
                discard = getattr(self.backend, "discard_response", None)
                if discard:
                    discard(getattr(text, "reuse_token", None))
                raise
        usage = stats.get("usage") if stats else None
        if usage:
            response["usage"] = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                                 "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                                 "input_tokens_details": {"cached_tokens": usage["cache_read_tokens"]}}
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

    async def _run_job(self, digest, shell, body, history, prompt, stats, images, live):
        began = time.monotonic()
        outcome = "completed"
        try:
            result = await self._generate(shell, body, history, prompt, stats, images, live)
            if self.ledger:
                self.ledger.put(digest, result, stats)
            return result
        except BaseException as exc:
            if isinstance(exc, InvalidRequest):
                stats["request_error"] = exc.code
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
                    subscriber = concurrent.futures.Future()
                    subscriber.live_output = future.live_output
                    def joined(done):
                        stats.update(prior_stats, dedup="joined")
                        if subscriber.cancelled():
                            return
                        if done.cancelled():
                            subscriber.cancel()
                        elif done.exception() is not None:
                            subscriber.set_exception(done.exception())
                        else:
                            result = done.result()
                            self._remember(body, history, result)
                            subscriber.set_result(result)
                    future.add_done_callback(joined)
                    return subscriber
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
            live = LiveOutput()
            future = asyncio.run_coroutine_threadsafe(
                self._run_job(digest, shell, body, history, prompt, stats, images, live), self.loop)
            future.live_output = live
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
            from cursor_sdk_bridge.version import running_version
            extra_capabilities = ["native_tools", "live_deltas"] if self.server.service.mode == "native" else []
            if self.server.service.ledger:
                extra_capabilities += ["durable_dedup", "disconnect_continuation"]
            self._json(200, {"running_version": running_version(), "mode": self.server.service.mode,
                             **self.server.service.lifecycle(), "service": "cursor-sdk2api", "status": "ready", "adapter_version": 3,
                             "capabilities": ["namespace_functions", "image_inputs", "structured_text_outputs", "safe_error_paths",
                                              "failure_labels", "data_keepalive", "sdk_progress", "anthropic_messages"] + extra_capabilities,
                             "progress": progress() if progress else None,
                             "limits": limits() if limits else None,
                             "prompt_limits": {context: self.server.service.context_limit(context) for context in CONTEXTS}})
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
            # A forced drain closes admission anyway: the operator is dropping unfinished work.
            force = self.path == "/admin/drain?force=1"
            with service.submit_lock:
                state = service.lifecycle()
                busy = path == "/admin/drain" and state["unfinished"] > 0 and not force
                # A refused drain must not block tool results needed by an
                # existing run. Close admission only after proving zero work.
                if not busy:
                    service.draining = path == "/admin/drain"
                state = service.lifecycle()
            return self._json(409 if busy else 200, state)
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
            service.record(**({"event": "http"} if service.ledger else {}), **api, outcome="invalid_request:" + str(exc.code))
            return fail(400, str(exc), exc.code, exc.param)
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError):
            service.record(**({"event": "http"} if service.ledger else {}), **api, outcome="invalid_request:malformed_request")
            return fail(400, "malformed JSON or request shape", "malformed_request")
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": estimate_tokens(len(prompt.encode()), images)})
        streaming = body.get("stream", False)
        entry = {**api, "model": body["model"], "stream": streaming, "input_items": len(history),
                 "prompt_bytes": len(prompt.encode()), "images": len(images),
                 "image_bytes": sum(image["bytes"] for image in images)}
        if not service.admission.acquire(blocking=False):
            service.record(**({"event": "http"} if service.ledger else {}), **entry, outcome="queue_full", duration_s=0)
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

        limit = service.prompt_limit(body["model"])
        try:
            if entry["prompt_bytes"] <= limit:
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
            if entry["prompt_bytes"] > limit:
                service.abandon(history)
                raise PromptTooLarge()
            live = ResponsesLive(emit) if streaming else None
            result, streamed_items = self._wait_inference(future, live,
                lambda: emit("response.in_progress", {"response": shell}))
            if streaming:
                for kind, fields in completion_events(result, start_index=streamed_items):
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
                    self._json(400 if isinstance(exc, (PromptTooLarge, InvalidRequest)) else 502, {"error": error})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            service.admission.release()
            service.record(**({"event": "http"} if service.ledger else {}), **entry, **stats, outcome=outcome, duration_s=round(time.monotonic() - began, 3),
                           bridge_launches=getattr(service.backend, "launches", None))

    def _wait_inference(self, future, live, keepalive):
        buffer = getattr(future, "live_output", None)
        cursor, count = 0, 0
        heartbeat = time.monotonic() + KEEPALIVE_SECONDS
        while True:
            if live and buffer:
                cursor, events = buffer.read(cursor)
                for event in events:
                    live.delta(event)
                    count = max(count, event["index"] + 1)
            if future.done():
                # A producer can finish between the read and done check.
                if live and buffer:
                    cursor, events = buffer.read(cursor)
                    for event in events:
                        live.delta(event)
                        count = max(count, event["index"] + 1)
                result = future.result()
                if live:
                    live.close_item()
                return result, count
            concurrent.futures.wait([future], timeout=min(.05, KEEPALIVE_SECONDS))
            if live and time.monotonic() >= heartbeat:
                keepalive()
                heartbeat = time.monotonic() + KEEPALIVE_SECONDS

    def _messages(self, service, body, request, history, prompt, images, entry, began):
        message, streaming = message_shell(body["model"]), entry["stream"]
        future, started, outcome, stats = None, False, "completed", {}

        def emit(kind, data):
            self.wfile.write(("event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

        limit = service.prompt_limit(body["model"])
        try:
            # Like the Anthropic API, oversized prompts fail with HTTP 400 before any stream starts.
            if entry["prompt_bytes"] > limit:
                service.abandon(history)
                raise PromptTooLarge()
            shell = response_shell(body["model"])
            future = service.submit(shell, request, history, prompt, stats, images)
            message["id"] = "msg_" + shell["id"].removeprefix("resp_")
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                started = True
                emit("message_start", {"type": "message_start", "message": message})
            response, streamed_items = self._wait_inference(future, MessagesLive(emit) if streaming else None,
                lambda: emit("ping", {"type": "ping"}))
            result = complete_message(message, response, stats.get("usage"))
            if streaming:
                for kind, data in stream_events(result, start_index=streamed_items):
                    emit(kind, data)
            else:
                self._json(200, result)
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client_disconnected"
            # The service owns inference; HTTP disconnect never cancels it.
        except Exception as exc:
            outcome = failure_label(exc)
            status, error = failure_error(outcome, entry["prompt_bytes"], limit)
            try:
                if started:
                    emit("error", error)
                else:
                    self._json(status, error, NO_RETRY)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            service.admission.release()
            service.record(**({"event": "http"} if service.ledger else {}), **entry, **stats, outcome=outcome, duration_s=round(time.monotonic() - began, 3),
                           bridge_launches=getattr(service.backend, "launches", None))


def make_server(service, port=8789):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.service = service
    return server


def main():
    from cursor_sdk_bridge.sdk_backend import DEFAULT_MAX_CONCURRENCY, DEFAULT_QUEUE_TIMEOUT, DEFAULT_TIMEOUTS, SDKBackend

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--mode", choices=("legacy", "reuse", "native"), default="native")
    parser.add_argument("--pending-timeout", type=float, default=600)
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
    parser.add_argument("--max-concurrency", type=int,
                        default=int(os.environ.get("CURSOR_FALLBACK_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)),
                        help="Concurrent SDK inferences; later requests queue "
                             f"(env CURSOR_FALLBACK_MAX_CONCURRENCY, default {DEFAULT_MAX_CONCURRENCY})")
    parser.add_argument("--max-prompt-bytes", type=int,
                        default=int(os.environ.get("CURSOR_FALLBACK_MAX_PROMPT_BYTES", MAX_PROMPT_BYTES)),
                        help="Reject larger prompts as context_length_exceeded (env CURSOR_FALLBACK_MAX_PROMPT_BYTES)")
    for context in CONTEXTS:
        variable = "CURSOR_FALLBACK_MAX_PROMPT_BYTES_" + context.upper()
        parser.add_argument("--max-prompt-bytes-" + context, type=int,
                            default=int(os.environ[variable]) if os.environ.get(variable) else None,
                            help=f"Lower limit for {context}-context models (env {variable})")
    parser.add_argument("--log-dir", type=Path, help="Private request metadata log directory (default STATE_DIR/logs)")
    parser.add_argument("--route", choices=("proxychains", "ambient", "direct"), default="proxychains")
    args = parser.parse_args()
    timeouts = {effort: args.timeout if args.timeout is not None else getattr(args, "timeout_" + effort)
                for effort in DEFAULT_TIMEOUTS}
    if not all(0 < seconds <= MAX_TIMEOUT for seconds in timeouts.values()):
        parser.error(f"timeouts must be greater than 0 and at most {MAX_TIMEOUT} seconds")
    if not 0 < args.queue_timeout <= MAX_TIMEOUT:
        parser.error(f"--queue-timeout must be greater than 0 and at most {MAX_TIMEOUT} seconds")
    if not 0 < args.max_concurrency <= MAX_ADMISSION:
        parser.error(f"--max-concurrency must be between 1 and {MAX_ADMISSION}")
    if args.max_prompt_bytes <= 0:
        parser.error("--max-prompt-bytes must be positive")
    context_prompt_bytes = {context: getattr(args, "max_prompt_bytes_" + context) for context in CONTEXTS
                            if getattr(args, "max_prompt_bytes_" + context) is not None}
    if any(value <= 0 for value in context_prompt_bytes.values()):
        parser.error("--max-prompt-bytes-<context> must be positive")
    os.umask(0o077)
    backend_type = SDKBackend
    if args.mode == "reuse":
        from cursor_sdk_bridge.reuse_backend import ReuseSDKBackend
        backend_type = ReuseSDKBackend
    elif args.mode == "native":
        from cursor_sdk_bridge.native_backend import NativeSDKBackend
        backend_type = NativeSDKBackend
    if not 0 < args.pending_timeout <= MAX_TIMEOUT or args.dedup_ttl <= 0:
        parser.error("pending timeout must be positive and at most 1800; dedup TTL must be positive")
    extra = {"pending_timeout": args.pending_timeout} if args.mode == "native" else {}
    backend = backend_type(args.key_file, args.state_dir / "workspace", route=args.route, timeouts=timeouts,
                         queue_timeout=args.queue_timeout, max_concurrency=args.max_concurrency, **extra)
    service = Service(backend, log=RequestLog(args.log_dir or args.state_dir / "logs"),
                      max_prompt_bytes=args.max_prompt_bytes, context_prompt_bytes=context_prompt_bytes, mode=args.mode,
                      ledger_path=None if args.no_ledger else args.state_dir / "results.sqlite3",
                      ledger_ttl=args.dedup_ttl)
    server = make_server(service, args.port)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print(f"cursor-sdk2api listening on 127.0.0.1:{server.server_port}; deadlines {timeouts}; queue {args.queue_timeout}; concurrency {args.max_concurrency}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
