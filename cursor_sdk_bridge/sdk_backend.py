"""One shared native bridge; concurrent independent agents in a shared empty workspace."""
import asyncio
import collections
import contextlib
import contextvars
from pathlib import Path
import time

from cursor_sdk_bridge.failures import DeadlineExpired, IsolationFailed, KeyInvalid, ModelMismatch, QueueTimeout, UpstreamIncomplete
from cursor_sdk_bridge.request_log import CURRENT_JOB, REQUEST_STATS
from cursor_sdk_bridge.models import EFFORTS, MODELS
from cursor_sdk_bridge.sdk_support import NATIVE_TOOL_EVENTS, bridge_command, model_identity, snapshot

DEFAULT_TIMEOUTS = {effort: 1200 for effort in EFFORTS}
DEFAULT_QUEUE_TIMEOUT = 1200
DEFAULT_MAX_CONCURRENCY = 3
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_RUN = contextvars.ContextVar("sdk_run", default=None)


class Slots:
    """First-come inference slots. A forced slot may exceed the limit; new runs then wait below it."""

    def __init__(self, limit):
        self.limit, self.busy, self.waiters = limit, 0, collections.deque()

    async def acquire(self):
        if self.busy < self.limit and not self.waiters:
            self.busy += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                # The slot was handed over as this waiter was cancelled.
                self.release()
            else:
                with contextlib.suppress(ValueError):
                    self.waiters.remove(waiter)
            raise

    def force(self):
        self.busy += 1

    def release(self):
        self.busy -= 1
        while self.waiters and self.busy < self.limit:
            waiter = self.waiters.popleft()
            if not waiter.done():
                self.busy += 1
                waiter.set_result(None)


class SDKBackend:
    def __init__(self, key_file, workspace, timeout=180, route="proxychains", timeouts=None,
                 queue_timeout=DEFAULT_QUEUE_TIMEOUT, max_concurrency=DEFAULT_MAX_CONCURRENCY):
        self.key_file, self.workspace = Path(key_file), Path(workspace)
        self.timeouts = dict(timeouts or {effort: timeout for effort in EFFORTS})
        self.timeout, self.route = max(self.timeouts.values()), route
        self.queue_timeout, self.max_concurrency = queue_timeout, max_concurrency
        self.client = self.http = self.process = self.drain = None
        self.launches = 0
        self.slots = Slots(max_concurrency)
        self.starting = asyncio.Lock()
        # Runs share one bridge. A failed run retires its bridge from new
        # admissions; the process closes once no remaining run still uses it.
        self.waiting, self.running, self.retiring = 0, {}, []

    def deadline(self, model):
        return self.timeouts[MODELS[model].effort]

    def _view(self, record, now):
        return {"model": record["model"], "running_s": round(now - record["started"], 1),
                "events": record["events"], "idle_s": round(now - record["last"], 1),
                "deadline_s": self.deadline(record["model"])}

    def progress(self):
        # Called from HTTP threads while the loop runs; metadata only.
        now = time.monotonic()
        runs = [self._view(record, now) for record in
                sorted(list(self.running.values()), key=lambda record: record["started"])]
        return {"queued": self.waiting, "active": runs[0] if runs else None, "runs": runs}

    def limits(self):
        return {"deadlines": dict(self.timeouts), "queue_timeout": self.queue_timeout,
                "max_concurrency": self.max_concurrency}

    def _touch(self, record):
        if record is not None:
            record["events"] += 1
            record["last"] = time.monotonic()

    def _client(self):
        record = _RUN.get()
        return record["client"] if record is not None and record["client"] is not None else self.client

    async def _admit(self, model):
        # Queue wait and inference have separate bounds: waiting behind other
        # requests must not consume this request's inference deadline.
        queue = asyncio.timeout(self.queue_timeout)
        job = CURRENT_JOB.get()
        if job is not None:
            job.queued = True
        self.waiting += 1
        try:
            async with queue:
                await self.slots.acquire()
        except TimeoutError:
            if queue.expired():
                raise QueueTimeout() from None
            raise
        finally:
            self.waiting -= 1
            if job is not None:
                job.queued = False
        now = time.monotonic()
        record = {"model": model, "started": now, "last": now, "events": 0, "client": None}
        self.running[id(record)] = record
        return record

    def _park(self, record):
        # A run waiting for client tool results does no inference, so its slot admits the next request.
        if record is not None and not record.get("parked"):
            record["parked"] = True
            self.slots.release()

    def _unpark(self, record):
        # Resuming must not wait behind new requests: the paused SDK callback has its own pending timeout.
        if record is not None and record.get("parked"):
            record["parked"] = False
            self.slots.force()

    async def _release(self, record):
        self.running.pop(id(record), None)
        if not record.get("parked"):
            self.slots.release()
        await self._reap()

    def _detach(self):
        handle = (self.client, self.process, self.http, self.drain)
        self.client = self.process = self.http = self.drain = None
        return handle

    def _retire(self, client):
        # A failed send can have no run handle while upstream work has started.
        # Stop routing new runs to that bridge; _reap closes it when idle.
        if client is not None and client is self.client:
            self.retiring.append(self._detach())

    async def _reap(self):
        used = {id(record["client"]) for record in self.running.values()}
        idle = [handle for handle in self.retiring if id(handle[0]) not in used]
        self.retiring = [handle for handle in self.retiring if id(handle[0]) in used]
        for handle in idle:
            await self._close_bridge(*handle)

    async def _start(self):
        from cursor_sdk import AsyncCursorClient, DefaultAsyncHttpxClient

        async with self.starting:
            if self.client is not None:
                if self.process is None or self.process.returncode is None:
                    return self.client
                self.retiring.append(self._detach())
            self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                private = (not self.key_file.is_symlink() and self.key_file.is_file()
                           and not self.key_file.stat().st_mode & 0o077)
                self.key = self.key_file.read_text().strip() if private else ""
            except OSError:
                self.key = ""
            if not self.key:
                raise KeyInvalid()
            http = DefaultAsyncHttpxClient(trust_env=False)
            launch = asyncio.create_task(AsyncCursorClient.launch_bridge(
                    command=bridge_command(self.route, "http1"), workspace=self.workspace,
                    timeout=15, client_timeout=self.timeout, max_retries=0,
                    http_client=http, allow_api_key_env_fallback=False,
                ))
            client = process = None
            try:
                # SDK launch does not clean up on CancelledError. Shield discovery,
                # retain its bounded task, then own/close any resulting process.
                client = await asyncio.shield(launch)
                process = client._owned_bridge.process
                self.client, self.process, self.http = client, process, http
                self.drain = asyncio.create_task(self._discard_stderr(process))
                self.launches += 1
                return client
            except BaseException:
                with contextlib.suppress(Exception):
                    client = await launch
                    process = client._owned_bridge.process
                if client is self.client:
                    self._detach()
                await self._close_bridge(client, process, http, None)
                raise

    async def _discard_stderr(self, process):
        stream = process.stderr
        if stream:
            while await stream.read(65536):
                pass

    async def _probe_auth_failure(self, error, client=None):
        """Classify a denied request using a free, read-only key check first.

        Never retry the inference. A valid key may still lack a model/region
        permission, so do not label every 403 as an invalid credential.
        """
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        if status not in (401, 403) and code not in ("unauthenticated", "permission_denied"):
            return
        client = client or self.client
        if client is None:
            return
        stats = REQUEST_STATS.get()
        verdict = "unknown"
        try:
            await asyncio.wait_for(client.me(api_key=self.key), 10)
            verdict = "valid"
        except Exception as probe_error:
            if getattr(probe_error, "status_code", None) == 401 or getattr(probe_error, "code", None) == "unauthenticated":
                verdict = "invalid"
        if stats is not None:
            stats["credential_probe"] = verdict
        if verdict == "invalid":
            raise KeyInvalid() from None

    async def generate(self, model, prompt, images=()):
        # Never replay an ambiguous failed send.
        waited = time.monotonic()
        record = await self._admit(model)
        context = _RUN.set(record)
        try:
            stats = REQUEST_STATS.get()
            if stats is not None:
                stats["queue_s"] = round(record["started"] - waited, 3)
            record["client"] = await self._start()
            scope = asyncio.timeout(self.deadline(model))
            try:
                async with scope:
                    return await self._generate(model, prompt, images)
            except BaseException as exc:
                try:
                    await self._probe_auth_failure(exc, record["client"])
                finally:
                    self._retire(record["client"])
                if isinstance(exc, TimeoutError) and scope.expired():
                    raise DeadlineExpired() from None
                raise
        finally:
            _RUN.reset(context)
            await self._release(record)

    async def _generate(self, model, prompt, images=()):
        from cursor_sdk import AgentOptions, LocalAgentOptions, SDKImage, SendOptions, UserMessage

        selection = MODELS[model].selection()
        native, record = [], _RUN.get()

        def observe(event):
            self._touch(record)
            kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            if kind in NATIVE_TOOL_EVENTS:
                native.append(kind)

        before = snapshot(self.workspace)
        agent = run = None
        try:
            agent = await self._client().agents.create(AgentOptions(
                api_key=self.key, model=selection, tools=[], disallowed_tools=["mcp", "task", "shell"],
                local=LocalAgentOptions(cwd=str(self.workspace), setting_sources=[])))
            message = UserMessage(text=prompt, images=[
                SDKImage.from_data(image["data"], image["mime_type"], dimension=image["dimension"])
                for image in images]) if images else prompt
            run = await agent.send(message, SendOptions(on_delta=observe, on_step=observe))
            full = done = False
            async for event in run.events():
                self._touch(record)
                full |= bool(event.result_is_full)
                done |= event.kind == "done"
            result = await run.wait()
            if not full or not done or result.status != "finished":
                raise UpstreamIncomplete()
            if result.model is None or model_identity(result.model.to_json()) != model_identity(selection):
                raise ModelMismatch()
            if native or snapshot(self.workspace) != before:
                raise IsolationFailed()
            stats, usage = REQUEST_STATS.get(), getattr(result, "usage", None)
            if stats is not None and usage is not None:
                stats["usage"] = {field: getattr(usage, field) for field in USAGE_FIELDS}
            return result.result
        finally:
            # A cancellation during send may have no run handle; bridge retirement
            # is the fallback in generate(). Cleanup never replaces the primary error.
            for target, method in ((run, "cancel"), (agent, "close")):
                if target is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(getattr(target, method)(), 3)

    async def close(self):
        handles, self.retiring = [self._detach(), *self.retiring], []
        for handle in handles:
            await self._close_bridge(*handle)

    async def _close_bridge(self, client, process, http, drain):
        owned = client._owned_bridge if client else None
        if client:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(client.aclose(), 4)
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if drain:
            drain.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await drain
        # Pinned SDK skips callback cleanup if its shutdown HTTP call is
        # cancelled; close saved callback servers even after ownership clears.
        if owned:
            for name in ("_store_callback_server", "_tool_callback_server"):
                callback = getattr(owned, name, None)
                if callback:
                    with contextlib.suppress(Exception):
                        callback.close()
                    setattr(owned, name, None)
        if http:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(http.aclose(), 3)
