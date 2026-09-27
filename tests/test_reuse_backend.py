import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cursor_sdk_bridge.failures import DeadlineExpired, IsolationFailed, QueueTimeout, UpstreamIncomplete
from cursor_sdk_bridge.request_log import REQUEST_STATS
from cursor_sdk_bridge.responses_protocol import complete_response, prepare_request, request_payload, response_shell
from cursor_sdk_bridge.reuse_backend import ReuseSDKBackend, canonical_history, policy_digest


MODEL = "claude-opus-5-5-high"
PNG = "data:image/png;base64,iVBORw0KGgo="
TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object"}}


class FakeRun:
    def __init__(self, client, agent, options):
        self.client, self.agent, self.options = client, agent, options
        self.cancelled = False

    async def events(self):
        self.options.on_delta({"type": "thinking-delta", "text": "synthetic thinking"})
        if self.client.native:
            self.options.on_step({"type": "toolCall"})
        if self.client.send_delay:
            await asyncio.sleep(self.client.send_delay)
        yield SimpleNamespace(kind="result", result_is_full=self.client.full)
        yield SimpleNamespace(kind="done", result_is_full=False)

    async def wait(self):
        return SimpleNamespace(status="finished", result=self.client.output,
            model=SimpleNamespace(to_json=lambda: self.agent.options.model),
            usage=SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_tokens=80, cache_write_tokens=20))

    async def cancel(self):
        self.cancelled = True


class FakeAgent:
    def __init__(self, client, agent_id, options):
        self.client, self.agent_id, self.options = client, agent_id, options
        self.closed = False

    async def send(self, message, options):
        self.client.sent.append((self.agent_id, message))
        if self.client.send_failure:
            raise RuntimeError("synthetic send failure")
        run = FakeRun(self.client, self, options)
        self.client.runs.append(run)
        return run

    async def close(self):
        self.closed = True


class FakeSdk:
    """The actual Python SDK's create/resume/send/events/wait/close contract."""
    def __init__(self):
        self.agents = self
        self._owned_bridge = SimpleNamespace(process=None, _tool_callback_server=None, _store_callback_server=None)
        self.created, self.resumed, self.sent, self.runs = [], [], [], []
        self.output = '{"output":[{"type":"message","text":"answer"}]}'
        self.resume_failure = self.send_failure = self.native = False
        self.full = True
        self.send_delay = 0

    async def create(self, options):
        agent = FakeAgent(self, "agent-" + str(len(self.created)), options)
        self.created.append(agent)
        return agent

    async def resume(self, agent_id, options):
        self.resumed.append(agent_id)
        if self.resume_failure:
            raise RuntimeError("synthetic unavailable checkpoint")
        # A closed Python handle does not delete persistent SDK agent context.
        self.assert_agent = next(agent for agent in self.created if agent.agent_id == agent_id)
        assert self.assert_agent.closed
        return FakeAgent(self, agent_id, options)

    async def aclose(self):
        pass


class ReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.backend = ReuseSDKBackend(root / "key", root / "workspace", timeout=1)
        self.backend.key = "synthetic-test-key"
        self.backend.workspace.mkdir()
        self.sdk = FakeSdk()
        self.backend.client = self.sdk

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def turn(self, history, *, body=None, commit=True, on_event=None):
        body = {"model": MODEL, "input": history, **(body or {})}
        history, prompt, images = prepare_request(body)
        stats = {}
        context = REQUEST_STATS.set(stats)
        try:
            text = await self.backend.generate_request(body["model"], prompt, body, history, images, on_event)
            response = complete_response(response_shell(body["model"]), text, body, history)
            if commit:
                self.assertTrue(self.backend.commit_response(history, body, response, text.reuse_token))
            return response, stats, text
        finally:
            REQUEST_STATS.reset(context)

    async def initial(self, **kwargs):
        history = [{"role": "user", "content": "first secret turn"}]
        response, stats, text = await self.turn(history, **kwargs)
        return history, response, stats, text

    async def test_every_variant_reaches_reuse_sdk_unchanged(self):
        from test_models import VARIANTS, expected_selection
        for alias, context, effort, fast in VARIANTS:
            with self.subTest(model=alias):
                await self.turn([{"role": "user", "content": "hello"}], body={"model": alias})
                self.assertEqual(self.sdk.created[-1].options.model,
                                 expected_selection(context, effort, fast))

    async def test_direct_successor_resumes_with_new_input_and_reports_usage(self):
        history, response, _, _ = await self.initial()
        self.assertTrue(self.sdk.created[0].closed)
        follow = history + response["output"] + [{"role": "user", "content": "next turn"}]
        _, stats, _ = await self.turn(follow)
        self.assertEqual(self.sdk.resumed, ["agent-0"])
        self.assertEqual(len(self.sdk.created), 1)
        sent = self.sdk.sent[-1][1]
        self.assertNotIn("first secret turn", sent)
        self.assertEqual(request_payload(sent)["input"], [{"role": "user", "content": "next turn"}])
        self.assertEqual(stats["reuse_mode"], "resume")
        self.assertEqual(stats["usage"]["cache_read_tokens"], 80)
        options = self.sdk.created[0].options
        self.assertEqual(options.local.store, {"type": "sqlite"})
        self.assertEqual(options.tools, [])
        self.assertEqual(options.local.setting_sources, [])

    async def test_transport_ids_status_and_text_wrappers_do_not_prevent_resume(self):
        history, response, _, _ = await self.initial()
        assistant = {"id": "new-id", "role": "assistant", "status": "in_progress", "content": "answer"}
        follow = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first secret turn"}]}]
        follow += [assistant, {"role": "user", "content": "next"}]
        await self.turn(follow)
        self.assertEqual(self.sdk.resumed, ["agent-0"])

    async def test_reasoning_display_blocks_may_be_added_or_omitted_on_replay(self):
        history, response, _, text = await self.initial(commit=False)
        response["output"].insert(0, {"type": "reasoning", "id": "reasoning-id", "summary": []})
        self.assertTrue(self.backend.commit_response(history, {"model": MODEL}, response, text.reuse_token))
        await self.turn(history + response["output"][1:] + [{"role": "user", "content": "next"}])
        self.assertEqual(self.sdk.resumed, ["agent-0"])

    async def test_replayed_trailing_reasoning_is_excluded_from_new_input(self):
        history, response, _, _ = await self.initial()
        follow = history + response["output"] + [{"type": "reasoning", "summary": []},
                                                 {"role": "user", "content": "next"}]
        await self.turn(follow)
        self.assertEqual(self.sdk.resumed, ["agent-0"])
        self.assertEqual(request_payload(self.sdk.sent[-1][1])["input"], [{"role": "user", "content": "next"}])

    async def test_unvalidated_output_never_becomes_parent(self):
        history, response, _, text = await self.initial(commit=False)
        await self.turn(history + response["output"] + [{"role": "user", "content": "next"}])
        self.assertEqual(self.sdk.resumed, [])
        self.backend.discard_response(text.reuse_token)
        self.assertNotIn(text.reuse_token, self.backend._unvalidated)

    async def test_commit_requires_matching_receipt_history_policy_and_completed_response(self):
        for failure in ("history", "policy", "status", "token"):
            with self.subTest(failure=failure):
                history, response, _, text = await self.initial(commit=False)
                body = {"model": MODEL}
                token = text.reuse_token
                if failure == "history":
                    history = [{"role": "user", "content": "wrong"}]
                elif failure == "policy":
                    body["instructions"] = "changed"
                elif failure == "status":
                    response["status"] = "in_progress"
                else:
                    token = "wrong-token"
                self.assertFalse(self.backend.commit_response(history, body, response, token))
        self.assertEqual(self.backend._completed, {})

    async def test_fork_claim_can_advance_agent_only_once(self):
        history, response, _, _ = await self.initial()
        branches = [history + response["output"] + [{"role": "user", "content": value}] for value in ("branch A", "branch B")]
        await asyncio.gather(*(self.turn(branch) for branch in branches))
        self.assertEqual(self.sdk.resumed, ["agent-0"])
        self.assertEqual(len(self.sdk.created), 2)

    async def test_compaction_mismatch_and_extra_assistant_turn_rebuild_cold(self):
        for variant in ("compact", "rewrite", "assistant"):
            with self.subTest(variant=variant):
                history, response, _, _ = await self.initial()
                follow = history + response["output"]
                if variant == "compact":
                    follow = [{"role": "user", "content": "summary of prior work"}]
                elif variant == "rewrite":
                    follow[0] = {"role": "user", "content": "different original input"}
                else:
                    follow += [{"role": "assistant", "content": "extra changed history"}]
                await self.turn(follow + [{"role": "user", "content": "next"}])
        self.assertEqual(self.sdk.resumed, [])

    async def test_changed_model_instructions_tools_choice_or_parallel_rebuild(self):
        variants = [{"model": "claude-opus-5-5-max"}, {"instructions": "new"}, {"tools": [TOOL]},
                    {"tool_choice": "none"}, {"parallel_tool_calls": False}]
        for body in variants:
            history, response, _, _ = await self.initial()
            await self.turn(history + response["output"] + [{"role": "user", "content": "next"}], body=body)
        self.assertEqual(self.sdk.resumed, [])

    async def test_old_images_are_not_resent_and_new_images_start_at_attachment_one(self):
        history = [{"role": "user", "content": [{"type": "input_image", "image_url": PNG}]}]
        response, _, _ = await self.turn(history)
        next_input = {"role": "user", "content": [{"type": "input_text", "text": "new image"},
                                                    {"type": "input_image", "image_url": PNG}]}
        response2, _, _ = await self.turn(history + response["output"] + [next_input])
        message = self.sdk.sent[-1][1]
        self.assertEqual(len(message.images), 1)
        self.assertIn("[Attached image 1]", message.text)
        self.assertNotIn("[Attached image 2]", message.text)
        await self.turn(history + response["output"] + [next_input] + response2["output"]
                        + [{"role": "user", "content": "no more images"}])
        self.assertIsInstance(self.sdk.sent[-1][1], str)

    async def test_namespace_tool_result_restores_sdk_call_id_after_client_renames_it(self):
        tools = [{"type": "namespace", "name": "functions", "tools": [TOOL]}]
        self.sdk.output = '{"output":[{"type":"function_call","call_id":"sdk-id","name":"functions.lookup","arguments":"{}"}]}'
        history, response, _, _ = await self.initial(body={"tools": tools})
        replay = copy.deepcopy(response["output"])
        replay[0]["call_id"] = "client-id"
        self.sdk.output = '{"output":[{"type":"message","text":"finished"}]}'
        await self.turn(history + replay + [{"type": "function_call_output", "call_id": "client-id", "output": "result"}],
                        body={"tools": tools})
        self.assertEqual(self.sdk.resumed, ["agent-0"])
        sent = request_payload(self.sdk.sent[-1][1])["input"]
        self.assertEqual(sent[0]["call_id"], "sdk-id")

    async def test_resume_failure_before_send_rebuilds_and_never_releases_claim(self):
        history, response, _, _ = await self.initial()
        self.sdk.resume_failure = True
        _, stats, _ = await self.turn(history + response["output"] + [{"role": "user", "content": "next"}])
        self.assertEqual(stats["reuse_reason"], "resume_unavailable")
        self.assertEqual(len(self.sdk.created), 2)
        self.assertIn("first secret turn", self.sdk.sent[-1][1])

    async def test_ambiguous_send_is_not_retried(self):
        history, response, _, _ = await self.initial()
        self.sdk.send_failure = True
        with self.assertRaisesRegex(RuntimeError, "synthetic send failure"):
            await self.turn(history + response["output"] + [{"role": "user", "content": "next"}])
        self.assertEqual(len(self.sdk.created), 1)
        self.assertEqual(len(self.sdk.sent), 2)
        self.assertEqual(self.backend._completed, {})

    async def test_queue_deadline_and_isolation_guards_still_apply(self):
        self.sdk.native = True
        with self.assertRaises(IsolationFailed):
            await self.initial()
        self.backend.client = self.sdk
        self.sdk.native, self.sdk.full = False, False
        with self.assertRaises(UpstreamIncomplete):
            await self.initial()
        self.backend.client = self.sdk
        self.sdk.full, self.sdk.send_delay = True, 0.1
        self.backend.timeouts["high"] = 0.01
        with self.assertRaises(DeadlineExpired):
            await self.initial()
        self.assertFalse(self.backend.lock.locked())
        self.assertIsNone(self.backend.active)
        self.assertEqual(self.backend._completed, {})

    async def test_queue_wait_does_not_bypass_parent_timeout(self):
        self.backend.queue_timeout = 0.01
        await self.backend.lock.acquire()
        try:
            with self.assertRaises(QueueTimeout):
                await self.initial()
        finally:
            self.backend.lock.release()
        self.assertEqual(self.backend.waiting, 0)
        self.assertEqual(self.sdk.created, [])

    async def test_lineage_ttl_and_capacity_bound_memory(self):
        self.backend.lineage_limit = 2
        for index in range(4):
            await self.turn([{"role": "user", "content": str(index)}])
        self.assertEqual(len(self.backend._completed), 2)
        self.backend.lineage_ttl = 0
        self.backend._prune()
        self.assertEqual(self.backend._completed, {})

    async def test_observer_receives_sdk_deltas_and_plain_generate_keeps_legacy_path(self):
        events = []
        await self.initial(on_event=events.append)
        self.assertEqual(events[0]["type"], "thinking-delta")
        await self.backend.generate(MODEL, "legacy prompt")
        self.assertEqual(len(self.sdk.created), 2)
        self.assertEqual(self.sdk.resumed, [])


class CanonicalTests(unittest.TestCase):
    def test_tool_result_order_and_call_relationships_remain_significant(self):
        calls = [{"type": "function_call", "call_id": value, "name": "lookup", "arguments": "{}"}
                 for value in ("a", "b")]
        first = calls + [{"type": "function_call_output", "call_id": "a", "output": "one"}]
        second = calls + [{"type": "function_call_output", "call_id": "b", "output": "one"}]
        self.assertNotEqual(canonical_history(first)[0], canonical_history(second)[0])

    def test_argument_key_order_and_adjacent_assistant_blocks_are_normalized(self):
        first = [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"},
                 {"type": "function_call", "call_id": "old", "name": "lookup", "arguments": '{"a":1,"b":2}'}]
        second = [{"role": "assistant", "content": [{"type": "output_text", "text": "ab"}]},
                  {"type": "function_call", "call_id": "new", "name": "lookup", "arguments": '{"b":2,"a":1}'}]
        self.assertEqual(canonical_history(first)[0], canonical_history(second)[0])

    def test_irrelevant_request_metadata_and_tool_order_do_not_change_policy(self):
        other = {**TOOL, "name": "other"}
        one = {"model": MODEL, "tools": [TOOL, other]}
        two = {"model": MODEL, "tools": [other, TOOL], "stream": True, "metadata": {"trace": "x"}}
        self.assertEqual(policy_digest(one), policy_digest(two))


if __name__ == "__main__":
    unittest.main()
