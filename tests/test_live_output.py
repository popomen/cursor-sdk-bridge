import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from cursor_bridge.cursor_sdk2api import Service, make_server
from cursor_bridge.failures import UpstreamIncomplete
from cursor_bridge.live_output import LiveOutput
from cursor_bridge.responses_protocol import complete_response, response_shell

MODEL = "claude-opus-5-5-high"


class StreamingBackend:
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0
        self.fail = False

    async def generate_request(self, model, prompt, body, history, images=(), on_event=None):
        self.calls += 1
        on_event({"type": "thinking_delta", "text": "Synthetic thinking."})
        on_event({"type": "text_delta", "text": "Hello "})
        while not self.release.is_set():
            await asyncio.sleep(.005)
        if self.fail:
            raise UpstreamIncomplete()
        on_event({"type": "text_delta", "text": "world."})
        return '{"output":[{"type":"message","text":"Hello world."}]}'

    async def close(self):
        pass


class LiveHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = StreamingBackend()
        self.service = Service(self.backend, mode="native", ledger_path=Path(self.temp.name) / "results.sqlite3")
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.backend.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def connect(self, api):
        body = {"model": MODEL, "stream": True}
        body.update({"input": "synthetic"} if api == "responses" else {"messages": [{"role": "user", "content": "synthetic"}]})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("POST", "/v1/" + api, json.dumps(body))
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        return connection, response

    def exercise(self, api):
        connection, response = self.connect(api)
        events = []
        try:
            while True:
                line = response.readline().decode()
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    is_text = (event["type"] == "response.output_text.delta" or
                               (isinstance(event.get("delta"), dict) and event["delta"].get("type") == "text_delta"))
                    if is_text:
                        break
                self.assertTrue(line)
            # Actual content arrived while the SDK was blocked, not a keepalive.
            self.assertFalse(self.backend.release.is_set())
            self.backend.release.set()
            events += [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
            return events
        finally:
            connection.close()

    def test_responses_streams_before_completion_and_keeps_ids(self):
        events = self.exercise("responses")
        self.assertIn("response.reasoning_summary_text.delta", [event["type"] for event in events])
        deltas = [event for event in events if event["type"] == "response.output_text.delta"]
        self.assertEqual("".join(event["delta"] for event in deltas), "Hello world.")
        final = events[-1]["response"]
        self.assertEqual(final["output"][1]["id"], deltas[0]["item_id"])
        self.assertEqual(final["output"][1]["content"][0]["text"], "Hello world.")
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
        connection, response = self.connect("responses")
        replay = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith("data: ")]
        connection.close()
        self.assertEqual(replay[-1]["response"], final)
        self.assertEqual(self.backend.calls, 1)

    def test_messages_streams_thinking_text_and_terminal_blocks_once(self):
        events = self.exercise("messages")
        starts = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual([event["content_block"]["type"] for event in starts], ["thinking", "text"])
        self.assertEqual([event["index"] for event in starts], [0, 1])
        text = "".join(event.get("delta", {}).get("text", "") for event in events)
        self.assertEqual(text, "Hello world.")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual(sum(event["type"] == "content_block_stop" for event in events), 2)

    def test_partial_stream_failure_never_claims_completed(self):
        self.backend.fail = True
        events = self.exercise("responses")
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("response.completed", [e["type"] for e in events])


class TranscriptTests(unittest.TestCase):
    def test_interleaved_segments_and_final_text_are_identical(self):
        live = LiveOutput()
        live.append({"type": "text_delta", "text": "one"})
        live.append({"type": "thinking_delta", "text": "synthetic"})
        live.append({"type": "text_delta", "text": "two"})
        response = complete_response(response_shell(MODEL), '{"output":[{"type":"message","text":"onetwo"}]}', {"model": MODEL}, [])
        result = live.finalize(response)
        self.assertEqual([item["type"] for item in result["output"]], ["message", "reasoning", "message"])

    def test_conflicting_final_text_is_not_wrapped_as_success(self):
        live = LiveOutput()
        live.append({"type": "text_delta", "text": "one"})
        response = complete_response(response_shell(MODEL), '{"output":[{"type":"message","text":"different"}]}', {"model": MODEL}, [])
        with self.assertRaises(UpstreamIncomplete):
            live.finalize(response)
