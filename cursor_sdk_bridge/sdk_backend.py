"""One owned native bridge; independent agents in a shared empty workspace."""
import asyncio
import contextlib
from pathlib import Path
import time

from cursor_sdk_bridge.failures import DeadlineExpired, IsolationFailed, KeyInvalid, ModelMismatch, QueueTimeout, UpstreamIncomplete
from cursor_sdk_bridge.request_log import REQUEST_STATS
from cursor_sdk_bridge.models import EFFORTS, MODELS
from cursor_sdk_bridge.sdk_support import NATIVE_TOOL_EVENTS, bridge_command, model_identity, snapshot

DEFAULT_TIMEOUTS = {effort: 1200 for effort in EFFORTS}
DEFAULT_QUEUE_TIMEOUT = 1200
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class SDKBackend:
    def __init__(self, key_file, workspace, timeout=180, route="proxychains", timeouts=None,
                 queue_timeout=DEFAULT_QUEUE_TIMEOUT):
        self.key_file, self.workspace = Path(key_file), Path(workspace)
        self.timeouts = dict(timeouts or {effort: timeout for effort in EFFORTS})
        self.timeout, self.route = max(self.timeouts.values()), route
        self.queue_timeout = queue_timeout
        self.client = self.http = self.process = self.drain = None
        self.launches = 0
        self.lock = asyncio.Lock()
        self.waiting, self.active = 0, None

    def deadline(self, model):
        return self.timeouts[MODELS[model].effort]

    def progress(self):
        # Called from HTTP threads while the loop runs; metadata only.
        active, now = self.active, time.monotonic()
        report = {"queued": self.waiting, "active": None}
        if active is not None:
            report["active"] = {"model": active["model"], "running_s": round(now - active["started"], 1),
                                "events": active["events"], "idle_s": round(now - active["last"], 1),
                                "deadline_s": self.deadline(active["model"])}
        return report

    def limits(self):
        return {"deadlines": dict(self.timeouts), "queue_timeout": self.queue_timeout}

    def _touch(self):
        active = self.active
        if active is not None:
            active["events"] += 1
            active["last"] = time.monotonic()

    async def _start(self):
        from cursor_sdk import AsyncCursorClient, DefaultAsyncHttpxClient

        if self.client is not None:
            return
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            private = (not self.key_file.is_symlink() and self.key_file.is_file()
                       and not self.key_file.stat().st_mode & 0o077)
            self.key = self.key_file.read_text().strip() if private else ""
        except OSError:
            self.key = ""
        if not self.key:
            raise KeyInvalid()
        self.http = DefaultAsyncHttpxClient(trust_env=False)
        launch = asyncio.create_task(AsyncCursorClient.launch_bridge(
                command=bridge_command(self.route, "http1"), workspace=self.workspace,
                timeout=15, client_timeout=self.timeout, max_retries=0,
                http_client=self.http, allow_api_key_env_fallback=False,
            ))
        try:
            # SDK launch does not clean up on CancelledError. Shield discovery,
            # retain its bounded task, then own/close any resulting process.
            self.client = await asyncio.shield(launch)
            self.process = self.client._owned_bridge.process
            self.drain = asyncio.create_task(self._discard_stderr())
            self.launches += 1
        except BaseException:
            with contextlib.suppress(Exception):
                self.client = await launch
                self.process = self.client._owned_bridge.process
            await self.close()
            raise

    async def _discard_stderr(self):
        stream = self.process.stderr
        if stream:
            while await stream.read(65536):
                pass

    async def _probe_auth_failure(self, error):
        """Classify a denied request using a free, read-only key check first.

        Never retry the inference. A valid key may still lack a model/region
        permission, so do not label every 403 as an invalid credential.
        """
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        if status not in (401, 403) and code not in ("unauthenticated", "permission_denied"):
            return
        if self.client is None:
            return
        stats = REQUEST_STATS.get()
        verdict = "unknown"
        try:
            await asyncio.wait_for(self.client.me(api_key=self.key), 10)
            verdict = "valid"
        except Exception as probe_error:
            if getattr(probe_error, "status_code", None) == 401 or getattr(probe_error, "code", None) == "unauthenticated":
                verdict = "invalid"
        if stats is not None:
            stats["credential_probe"] = verdict
        if verdict == "invalid":
            raise KeyInvalid() from None

    async def generate(self, model, prompt, images=()):
        # Queue wait and inference have separate bounds: waiting behind another
        # request must not consume this request's inference deadline. Never
        # replay an ambiguous failed send.
        waited = time.monotonic()
        queue = asyncio.timeout(self.queue_timeout)
        self.waiting += 1
        try:
            async with queue:
                await self.lock.acquire()
        except TimeoutError:
            if queue.expired():
                raise QueueTimeout() from None
            raise
        finally:
            self.waiting -= 1
        try:
            acquired = time.monotonic()
            self.active = {"model": model, "started": acquired, "last": acquired, "events": 0}
            stats = REQUEST_STATS.get()
            if stats is not None:
                stats["queue_s"] = round(acquired - waited, 3)
            await self._start()
            scope = asyncio.timeout(self.deadline(model))
            try:
                async with scope:
                    return await self._generate(model, prompt, images)
            except BaseException as exc:
                try:
                    await self._probe_auth_failure(exc)
                finally:
                    await self.close()
                if isinstance(exc, TimeoutError) and scope.expired():
                    raise DeadlineExpired() from None
                raise
        finally:
            self.active = None
            self.lock.release()

    async def _generate(self, model, prompt, images=()):
        from cursor_sdk import AgentOptions, LocalAgentOptions, SDKImage, SendOptions, UserMessage

        selection = MODELS[model].selection()
        native = []

        def observe(event):
            self._touch()
            kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            if kind in NATIVE_TOOL_EVENTS:
                native.append(kind)

        before = snapshot(self.workspace)
        agent = run = None
        try:
            agent = await self.client.agents.create(AgentOptions(
                api_key=self.key, model=selection, tools=[], disallowed_tools=["mcp", "task", "shell"],
                local=LocalAgentOptions(cwd=str(self.workspace), setting_sources=[])))
            message = UserMessage(text=prompt, images=[
                SDKImage.from_data(image["data"], image["mime_type"], dimension=image["dimension"])
                for image in images]) if images else prompt
            run = await agent.send(message, SendOptions(on_delta=observe, on_step=observe))
            full = done = False
            async for event in run.events():
                self._touch()
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
            # A cancellation during send may have no run handle; bridge shutdown is
            # the fallback in generate(). Cleanup never replaces the primary error.
            for target, method in ((run, "cancel"), (agent, "close")):
                if target is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(getattr(target, method)(), 3)

    async def close(self):
        client, process, http, drain = self.client, self.process, self.http, self.drain
        owned = client._owned_bridge if client else None
        self.client = self.process = self.http = self.drain = None
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
