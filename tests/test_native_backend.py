import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cursor_bridge.failures import DeadlineExpired, IsolationFailed, KeyInvalid, ModelMismatch, UpstreamIncomplete
from cursor_bridge.native_backend import NativeSDKBackend, completed_results, native_prompt
from cursor_bridge.request_log import REQUEST_STATS
from cursor_bridge.responses_protocol import InvalidRequest, complete_response, prepare_request, response_shell


MODEL = "claude-opus-5-5-high"
TOOL = {"type": "function", "name": "lookup", "parameters": {
    "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}


class FakeRun:
    def __init__(self, agent, options, script):
        self.agent, self.options, self.script = agent, options, script
        self.status, self.cancelled, self.text = "running", False, ""

    async def events(self):
        for kind, value in self.script:
            if kind in ("text", "thinking"):
                self.options.on_delta(SimpleNamespace(type=kind + "-delta", text=value))
                if kind == "text":
                    self.text += value
                yield SimpleNamespace(kind="interaction_update", result_is_full=False)
            elif kind == "tools":
                calls = []
                for index, (name, arguments) in enumerate(value):
                    tool = self.agent.options.local.custom_tools[name]
                    calls.append(tool.execute(arguments, SimpleNamespace(tool_call_id="sdk-" + str(index))))
                self.agent.client.callback_results.extend(await asyncio.gather(*calls))
            elif kind == "late_tools":
                calls = []
                for index, (name, arguments) in enumerate(value):
                    tool = self.agent.options.local.custom_tools[name]
                    calls.append(asyncio.create_task(tool.execute(arguments,
                        SimpleNamespace(tool_call_id="late-sdk-" + str(index)))))
                    if index == 0:
                        await asyncio.sleep(0.04)
                self.agent.client.callback_results.extend(await asyncio.gather(*calls))
            elif kind == "wait":
                await value.wait()
            elif kind == "sleep":
                await asyncio.sleep(value)
            elif kind == "write":
                (Path(self.agent.options.local.cwd) / "unexpected").write_text("synthetic")
            elif kind == "error":
                raise RuntimeError("synthetic SDK failure")
        self.status = "finished"
        yield SimpleNamespace(kind="result", result_is_full=self.agent.client.full)
        yield SimpleNamespace(kind="done", result_is_full=False)

    async def wait(self):
        model = self.agent.options.model if not self.agent.client.wrong_model else {"id": "wrong"}
        return SimpleNamespace(status=self.status, result=self.text,
            model=SimpleNamespace(to_json=lambda: model),
            usage=SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_tokens=80, cache_write_tokens=20))

    async def cancel(self):
        self.cancelled, self.status = True, "cancelled"


class FakeAgent:
    def __init__(self, client, agent_id, options):
        self.client, self.agent_id, self.options = client, agent_id, options
        self.closed = False

    async def send(self, message, options):
        self.client.sent.append((self.agent_id, message))
        if self.client.send_error:
            raise RuntimeError("synthetic failed send")
        run = FakeRun(self, options, self.client.scripts.pop(0))
        self.client.runs.append(run)
        return run

    async def close(self):
        self.closed = True


class FakeSdk:
    def __init__(self):
        self.agents = self
        self._owned_bridge = SimpleNamespace(process=None, _tool_callback_server=None, _store_callback_server=None)
        self.created, self.resumed, self.sent, self.runs, self.callback_results = [], [], [], [], []
        self.scripts = []
        self.full, self.wrong_model, self.resume_error, self.send_error = True, False, False, False
        self.auth_error, self.auth_invalid, self.auth_probes = False, False, 0

    async def create(self, options):
        if self.auth_error:
            error = RuntimeError("synthetic authentication failure")
            error.status_code = 401
            raise error
        agent = FakeAgent(self, "agent-" + str(len(self.created)), options)
        self.created.append(agent)
        return agent

    async def resume(self, agent_id, options):
        self.resumed.append(agent_id)
        if self.resume_error:
            raise RuntimeError("synthetic lost checkpoint")
        return FakeAgent(self, agent_id, options)

    async def aclose(self):
        pass

    async def me(self, **kwargs):
        self.auth_probes += 1
        if self.auth_invalid:
            error = RuntimeError("synthetic invalid credential")
            error.status_code = 401
            raise error
        return SimpleNamespace()


class NativeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.backend = NativeSDKBackend(root / "key", root / "workspace", timeout=2,
            pending_timeout=1, batch_settle=0.005)
        self.backend.key = "synthetic-key"
        self.backend.workspace.mkdir()
        self.sdk = FakeSdk()
        self.backend.client = self.sdk
        async def start_fake():
            if self.backend.client is None:
                self.backend.client = self.sdk
        self.backend._start = start_fake

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def turn(self, history, *, tools=(), commit=True, events=None, **options):
        body = {"model": MODEL, "input": history, "tools": list(tools), **options}
        history, prompt, images = prepare_request(body)
        stats = {}
        context = REQUEST_STATS.set(stats)
        try:
            text = await self.backend.generate_request(MODEL, prompt, body, history,
                images=images, on_event=events.append if events is not None else None)
            response = complete_response(response_shell(MODEL), text, body, history)
            if commit:
                self.assertTrue(self.backend.commit_response(history, body, response, text.reuse_token))
            return response, stats, text
        finally:
            REQUEST_STATS.reset(context)

    async def idle(self):
        for _ in range(100):
            if self.backend.pending_count() == 0:
                return
            await asyncio.sleep(0.005)
        self.fail("run did not become idle")

    async def test_native_text_streaming_and_resume_only_latest_input(self):
        self.sdk.scripts = [[("thinking", "consider"), ("text", "first answer")], [("text", "second answer")]]
        history, events = [{"role": "user", "content": "first private turn"}], []
        first, stats, _ = await self.turn(history, events=events)
        self.assertEqual([event["type"] for event in events], ["thinking_delta", "text_delta"])
        self.assertEqual(stats["reuse_mode"], "cold")
        await self.idle()
        history += first["output"] + [{"role": "user", "content": "new turn"}]
        second, stats, _ = await self.turn(history)
        self.assertEqual(stats["reuse_mode"], "resume")
        self.assertEqual(self.sdk.resumed, ["agent-0"])
        self.assertNotIn("first private turn", self.sdk.sent[-1][1])
        self.assertIn("new turn", self.sdk.sent[-1][1])
        self.assertEqual(second["output"][0]["content"][0]["text"], "second answer")
        options = self.sdk.created[0].options
        self.assertEqual(options.tools, [])
        self.assertIn("shell", options.disallowed_tools)
        self.assertEqual(options.local.setting_sources, [])

    async def test_parallel_callbacks_continue_same_run_and_new_event_sink(self):
        self.sdk.scripts = [[("text", "checking"), ("tools", [("lookup", {"key": "a"}),
                            ("lookup", {"key": "b"})]), ("thinking", "after results"), ("text", "done")]]
        history, events = [{"role": "user", "content": "look up two"}], []
        first, _, _ = await self.turn(history, tools=[TOOL], events=events)
        calls = [item for item in first["output"] if item["type"] == "function_call"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item["call_id"].startswith("toolu_") for item in calls))
        self.assertEqual(self.backend.progress()["pending_tools"], 2)
        self.assertEqual(self.backend.pending_count(), 1)
        self.assertEqual(self.sdk.created[0].options.tools, ["mcp"])
        self.assertNotIn("mcp", self.sdk.created[0].options.disallowed_tools)
        next_events = []
        history += first["output"] + [{"type": "function_call_output", "call_id": item["call_id"],
                                      "output": "result " + str(index)} for index, item in enumerate(calls)]
        final, stats, _ = await asyncio.wait_for(self.turn(history, tools=[TOOL], events=next_events), 1)
        self.assertEqual(len(self.sdk.created), 1)
        self.assertEqual(len(self.sdk.sent), 1)
        self.assertEqual(stats["reuse_mode"], "tool_continuation")
        self.assertEqual(final["output"][0]["content"][0]["text"], "done")
        self.assertEqual([event["text"] for event in next_events], ["after results", "done"])
        self.assertEqual([event["text"] for event in events], ["checking"])
        await self.idle()
        self.assertEqual(self.backend.progress()["pending_tools"], 0)

    async def test_multiple_sequential_tool_batches_share_one_run(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]),
                             ("tools", [("lookup", {"key": "b"})]), ("text", "all done")]]
        history = [{"role": "user", "content": "two sequential tools"}]
        for expected in ("a", "b"):
            result, _, _ = await self.turn(history, tools=[TOOL])
            call = result["output"][0]
            self.assertEqual(json.loads(call["arguments"])["key"], expected)
            history += result["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "ok"}]
        final, _, _ = await self.turn(history, tools=[TOOL])
        self.assertEqual(final["output"][0]["content"][0]["text"], "all done")
        self.assertEqual(len(self.sdk.sent), 1)

    async def test_cold_recovery_replays_occurrences_without_client_calls(self):
        history = [{"role": "user", "content": "recover"}]
        for index in range(2):
            history += [{"type": "function_call", "call_id": "old-" + str(index), "name": "lookup",
                         "arguments": '{"key":"same"}'},
                        {"type": "function_call_output", "call_id": "old-" + str(index), "output": "saved-" + str(index)}]
        self.sdk.scripts = [[("tools", [("lookup", {"key": "same"})]),
                            ("tools", [("lookup", {"key": "same"})]), ("text", "recovered")]]
        result, stats, _ = await self.turn(history, tools=[TOOL])
        self.assertEqual(result["output"][0]["type"], "message")
        self.assertEqual(stats["replayed_tools"], 2)
        self.assertEqual([value["content"][0]["text"] for value in self.sdk.callback_results], ["saved-0", "saved-1"])

    async def test_pending_expiry_releases_lock_and_late_result_recovers_cold(self):
        self.backend.pending_timeout = 0.03
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]), ("text", "expired")],
                            [("tools", [("lookup", {"key": "a"})]), ("text", "recovered")]]
        history = [{"role": "user", "content": "one tool"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        call = first["output"][0]
        await self.idle()
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertFalse(self.backend.lock.locked())
        history += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "saved"}]
        self.backend.client = self.sdk
        final, stats, _ = await self.turn(history, tools=[TOOL])
        self.assertEqual(stats["reuse_mode"], "cold")
        self.assertEqual(stats["replayed_tools"], 1)
        self.assertEqual(final["output"][0]["content"][0]["text"], "recovered")

    async def test_pending_mismatch_does_not_consume_results(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"}), ("lookup", {"key": "b"})]), ("text", "done")]]
        history = [{"role": "user", "content": "two tools"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        calls = first["output"]
        partial = history + calls + [{"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "a"}]
        with self.assertRaises(InvalidRequest):
            await self.turn(partial, tools=[TOOL])
        self.assertEqual(self.backend.progress()["pending_tools"], 2)
        complete = partial + [{"type": "function_call_output", "call_id": calls[1]["call_id"], "output": "b"}]
        with self.assertRaises(InvalidRequest):
            await self.turn(complete, tools=[TOOL], instructions="changed policy")
        await self.turn(complete, tools=[TOOL])

    async def test_branch_is_cold_after_first_successor_claim(self):
        self.sdk.scripts = [[("text", "parent")], [("text", "branch a")], [("text", "branch b")]]
        history = [{"role": "user", "content": "first"}]
        first, _, _ = await self.turn(history)
        history += first["output"]
        await self.turn(history + [{"role": "user", "content": "a"}])
        _, stats, _ = await self.turn(history + [{"role": "user", "content": "b"}])
        self.assertEqual(stats["reuse_mode"], "cold")
        self.assertEqual(len(self.sdk.resumed), 1)
        self.assertEqual(len(self.sdk.created), 2)

    async def test_namespace_and_images_use_native_sdk_shapes(self):
        from cursor_bridge.reuse_backend import _digest
        tool = {"type": "namespace", "name": "functions", "tools": [TOOL]}
        sdk_name = "bridge_" + _digest("functions.lookup")[:32]
        self.sdk.scripts = [[("tools", [(sdk_name, {"key": "a"})]), ("text", "done")]]
        history = [{"role": "user", "content": [{"type": "input_image", "image_url":
            "data:image/png;base64,iVBORw0KGgo="}, {"type": "input_text", "text": "look"}]}]
        first, _, _ = await self.turn(history, tools=[tool])
        call = first["output"][0]
        self.assertEqual((call["namespace"], call["name"]), ("functions", "lookup"))
        self.assertEqual(len(self.sdk.sent[0][1].images), 1)
        self.assertNotIn("base64", self.sdk.sent[0][1].text)
        await self.turn(history + first["output"] + [{"type": "function_call_output",
            "call_id": call["call_id"], "output": "yes"}], tools=[tool])

    async def test_reasoning_does_not_prevent_messages_resume(self):
        self.sdk.scripts = [[("text", "answer")], [("text", "next")]]
        history = [{"role": "user", "content": "first"}]
        body = {"model": MODEL, "input": history}
        normalized, prompt, images = prepare_request(body)
        text = await self.backend.generate_request(MODEL, prompt, body, normalized)
        response = complete_response(response_shell(MODEL), text, body, normalized)
        response["output"].insert(0, {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thought"}]})
        self.assertTrue(self.backend.commit_response(history, body, response, text.reuse_token))
        latest = history + [item for item in response["output"] if item["type"] != "reasoning"]
        latest.append({"role": "user", "content": "next"})
        _, stats, _ = await self.turn(latest)
        self.assertEqual(stats["reuse_mode"], "resume")

    async def test_resume_failure_rebuilds_before_send(self):
        self.sdk.scripts = [[("text", "first")], [("text", "second")]]
        history = [{"role": "user", "content": "old"}]
        first, _, _ = await self.turn(history)
        self.sdk.resume_error = True
        _, stats, _ = await self.turn(history + first["output"] + [{"role": "user", "content": "new"}])
        self.assertEqual(stats["reuse_reason"], "resume_unavailable")
        self.assertIn("old", self.sdk.sent[-1][1])
        self.assertEqual(len(self.sdk.sent), 2)

    async def test_isolation_model_and_full_terminal_validation(self):
        for failure in ("isolation", "model", "incomplete"):
            with self.subTest(failure=failure):
                self.sdk.scripts = [[("write", None), ("text", "answer")]] if failure == "isolation" else [[("text", "answer")]]
                self.sdk.wrong_model, self.sdk.full = failure == "model", failure != "incomplete"
                self.backend.client = self.sdk
                expected = {"isolation": IsolationFailed, "model": ModelMismatch, "incomplete": UpstreamIncomplete}[failure]
                with self.assertRaises(expected):
                    await self.turn([{"role": "user", "content": "validate"}])
                await self.idle()

    async def test_cancelled_waiter_does_not_cancel_background_run(self):
        gate = asyncio.Event()
        self.sdk.scripts = [[("wait", gate), ("text", "complete")]]
        waiter = asyncio.create_task(self.turn([{"role": "user", "content": "detach"}]))
        while not self.sdk.runs:
            await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.backend.pending_count(), 1)
        gate.set()
        await self.idle()
        self.assertFalse(self.sdk.runs[0].cancelled)

    async def test_discard_pending_response_cancels_owned_run(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]), ("text", "unused")]]
        _, _, text = await self.turn([{"role": "user", "content": "tool"}], tools=[TOOL], commit=False)
        self.backend.discard_response(text.reuse_token)
        await self.idle()
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertFalse(self.backend._by_call)
        self.assertIsNone(self.backend.client)

    async def test_deadline_cancels_run_and_releases_queue(self):
        self.backend.timeouts = {"high": 0.02, "xhigh": 0.02, "max": 0.02}
        self.sdk.scripts = [[("sleep", 1), ("text", "unused")]]
        with self.assertRaises(DeadlineExpired):
            await self.turn([{"role": "user", "content": "timeout"}])
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertFalse(self.backend.lock.locked())
        self.assertEqual(self.sdk.auth_probes, 0)

    async def test_each_tool_response_gets_a_fresh_inference_deadline(self):
        self.backend.timeouts = {effort: 0.1 for effort in ("high", "xhigh", "max")}
        self.sdk.scripts = [[("sleep", 0.06), ("tools", [("lookup", {"key": "a"})]),
                            ("sleep", 0.06), ("tools", [("lookup", {"key": "b"})]),
                            ("sleep", 0.06), ("text", "finished across several deadlines")]]
        history = [{"role": "user", "content": "three short inference segments"}]
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(2):
            response, _, _ = await self.turn(history, tools=[TOOL])
            session = next(iter(self.backend._sessions.values()))
            self.assertIsNone(session.scope.when())
            await asyncio.sleep(0.12)  # Each client wait exceeds the inference limit.
            self.assertEqual(self.backend.pending_count(), 1)
            history += response["output"] + [{"type": "function_call_output",
                "call_id": response["output"][0]["call_id"], "output": "complete"}]
        final, _, _ = await self.turn(history, tools=[TOOL])
        self.assertGreater(loop.time() - started, 0.3)
        self.assertEqual(len(self.sdk.sent), 1)
        self.assertEqual(final["output"][0]["type"], "message")
        self.assertFalse(self.sdk.runs[0].cancelled)

    async def test_bridge_start_does_not_consume_inference_deadline(self):
        self.backend.timeouts = {effort: 0.05 for effort in ("high", "xhigh", "max")}
        async def slow_start():
            await asyncio.sleep(0.08)
        self.backend._start = slow_start
        self.sdk.scripts = [[("text", "ready after bridge startup")]]
        response, _, _ = await self.turn([{"role": "user", "content": "startup"}])
        self.assertEqual(response["output"][0]["type"], "message")

    async def test_resumed_segment_still_times_out_when_inference_is_slow(self):
        self.backend.timeouts = {effort: 0.05 for effort in ("high", "xhigh", "max")}
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]),
                            ("sleep", 0.2), ("text", "too late")]]
        history = [{"role": "user", "content": "slow continuation"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        await asyncio.sleep(0.07)
        history += first["output"] + [{"type": "function_call_output",
            "call_id": first["output"][0]["call_id"], "output": "complete"}]
        with self.assertRaises(DeadlineExpired):
            await self.turn(history, tools=[TOOL])
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertFalse(self.backend.lock.locked())

    async def test_waiting_tool_has_pending_timeout_with_inference_clock_stopped(self):
        self.backend.timeouts = {effort: 0.2 for effort in ("high", "xhigh", "max")}
        self.backend.pending_timeout = 0.04
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]), ("text", "unused")]]
        await self.turn([{"role": "user", "content": "wait for external tool"}], tools=[TOOL])
        session = next(iter(self.backend._sessions.values()))
        self.assertIsNone(session.scope.when())
        await self.idle()
        self.assertIsInstance(session.error, DeadlineExpired)
        self.assertFalse(session.scope.expired())
        self.assertTrue(self.sdk.runs[0].cancelled)

    async def test_unreported_late_tool_keeps_inference_timer_paused(self):
        self.backend.timeouts = {effort: 0.025 for effort in ("high", "xhigh", "max")}
        self.sdk.scripts = [[("late_tools", [("lookup", {"key": "a"}),
                                             ("lookup", {"key": "late"})]), ("text", "done")]]
        history = [{"role": "user", "content": "delayed parallel callbacks"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        await asyncio.sleep(0.06)
        self.backend.batch_settle = 0.04  # Reporting the late tool exceeds inference allowance.
        history += first["output"] + [{"type": "function_call_output",
            "call_id": first["output"][0]["call_id"], "output": "first"}]
        second, _, _ = await self.turn(history, tools=[TOOL])
        session = next(iter(self.backend._sessions.values()))
        self.assertIsNone(session.scope.when())
        history += second["output"] + [{"type": "function_call_output",
            "call_id": second["output"][0]["call_id"], "output": "second"}]
        await self.turn(history, tools=[TOOL])
        self.assertEqual(len(self.sdk.sent), 1)

    async def test_auth_failure_probes_once_before_returning_key_invalid(self):
        self.sdk.auth_error = self.sdk.auth_invalid = True
        with self.assertRaises(KeyInvalid):
            await self.turn([{"role": "user", "content": "auth"}])
        self.assertEqual(self.sdk.auth_probes, 1)
        self.assertEqual(self.sdk.sent, [])

    async def test_none_tool_choice_removes_sdk_tools(self):
        self.sdk.scripts = [[("text", "no tools")]]
        await self.turn([{"role": "user", "content": "no tools"}], tools=[TOOL], tool_choice="none")
        self.assertEqual(self.sdk.created[0].options.tools, [])
        self.assertEqual(self.sdk.created[0].options.local.custom_tools, {})

    async def test_callback_schema_failure_does_not_expose_invalid_tool(self):
        self.sdk.scripts = [[("tools", [("lookup", {"unexpected": True})]), ("text", "unused")]]
        from jsonschema import ValidationError
        with self.assertRaises(ValidationError):
            await self.turn([{"role": "user", "content": "bad schema"}], tools=[TOOL])
        self.assertFalse(self.backend._by_call)
        self.assertEqual(self.backend.pending_count(), 0)

    async def test_late_callback_becomes_next_public_batch(self):
        self.sdk.scripts = [[("late_tools", [("lookup", {"key": "a"}),
                                              ("lookup", {"key": "late"})]), ("text", "done")]]
        history = [{"role": "user", "content": "delayed parallel tools"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        first_call = first["output"][0]
        await asyncio.sleep(0.06)
        self.assertEqual(self.backend.progress()["pending_tools"], 2)
        history += first["output"] + [{"type": "function_call_output",
                                      "call_id": first_call["call_id"], "output": "a result"}]
        second, _, _ = await self.turn(history, tools=[TOOL])
        late_call = second["output"][0]
        self.assertEqual(json.loads(late_call["arguments"]), {"key": "late"})
        history += second["output"] + [{"type": "function_call_output",
                                       "call_id": late_call["call_id"], "output": "late result"}]
        final, _, _ = await self.turn(history, tools=[TOOL])
        self.assertEqual(final["output"][0]["content"][0]["text"], "done")
        self.assertEqual(len(self.sdk.sent), 1)

    async def test_extra_user_input_rebuilds_and_replays_completed_tool(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"})]), ("text", "discarded")],
                            [("tools", [("lookup", {"key": "a"})]), ("text", "new instructions followed")]]
        history = [{"role": "user", "content": "initial"}]
        first, _, _ = await self.turn(history, tools=[TOOL])
        history += first["output"] + [{"type": "function_call_output",
            "call_id": first["output"][0]["call_id"], "output": "completed"},
            {"role": "user", "content": "<system-reminder>new user content</system-reminder>"}]
        final, stats, _ = await self.turn(history, tools=[TOOL])
        self.assertEqual(stats["reuse_reason"], "tool_results_with_new_user_input")
        self.assertEqual(stats["replayed_tools"], 1)
        self.assertEqual(len(self.sdk.sent), 2)
        self.assertIn("new user content", self.sdk.sent[-1][1])
        self.assertTrue(self.sdk.runs[0].cancelled)
        self.assertEqual(final["output"][0]["type"], "message")

    async def test_disabled_parallel_returns_one_callback_per_response(self):
        self.sdk.scripts = [[("tools", [("lookup", {"key": "a"}), ("lookup", {"key": "b"})]), ("text", "done")]]
        history = [{"role": "user", "content": "sequential delivery"}]
        for _ in range(2):
            response, _, _ = await self.turn(history, tools=[TOOL], parallel_tool_calls=False)
            self.assertEqual(len(response["output"]), 1)
            history += response["output"] + [{"type": "function_call_output",
                "call_id": response["output"][0]["call_id"], "output": "ok"}]
        await self.turn(history, tools=[TOOL], parallel_tool_calls=False)
        self.assertEqual(len(self.sdk.sent), 1)


if __name__ == "__main__":
    unittest.main()
