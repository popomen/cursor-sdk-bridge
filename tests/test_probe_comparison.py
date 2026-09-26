import copy
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from cursor_sdk_bridge import probe_comparison as probe


LEGACY = "http://127.0.0.1:18789"
NEW = "http://127.0.0.1:18790/v1/responses"


class FakeResponse(io.BytesIO):
    def __init__(self, data, content_type="text/event-stream"):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def event(kind, payload):
    return ("event: " + kind + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def completed(code, *, cache=2000, status="completed", reasoning=False):
    output = [{"id": "synthetic-response-id", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": code}]}]
    if reasoning:
        output.insert(0, {"type": "reasoning", "summary": [{"type": "summary_text", "text": "PRIVATE_THINKING"}]})
    return {"response": {"status": status, "output": output,
                         "usage": {"input_tokens": 30000, "output_tokens": 8,
                                   "input_tokens_details": {"cached_tokens": cache}}}}


def success_stream(code, *, cache=2000, reasoning=False):
    return (b": keepalive\n\n" + event("response.created", {"response": {"status": "in_progress"}})
            + event("response.reasoning_summary_text.delta", {"delta": "PRIVATE_THINKING"})
            + event("response.output_text.delta", {"delta": code})
            + event("response.completed", completed(code, cache=cache, reasoning=reasoning)))


class FakeOpener:
    def __init__(self, data, content_type="text/event-stream", error=None):
        self.data, self.content_type, self.error = data, content_type, error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.content_type)


def clock(values):
    iterator = iter(values)
    return lambda: next(iterator)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.body = probe.request_body([{"role": "user", "content": "synthetic request"}], "high")

    def test_arrival_times_usage_and_only_validated_output_match_are_recorded(self):
        opener = FakeOpener(success_stream("EXPECTED_CODE", reasoning=True))
        result = probe.stream_turn(NEW, self.body, "EXPECTED_CODE", opener=opener,
            clock=clock([0, 0.1, 0.2, 0.8, 1.2, 1.3]))
        report = result.metadata
        self.assertTrue(report["completed"])
        self.assertTrue(report["output_matches"])
        self.assertEqual(report["first_content_s"], 0.2)
        self.assertEqual(report["first_thinking_s"], 0.2)
        self.assertEqual(report["first_text_s"], 0.8)
        self.assertEqual(report["duration_s"], 1.3)
        self.assertTrue(report["streamed_before_completion"])
        self.assertEqual(report["usage"]["cache_read_tokens"], 2000)
        self.assertIsNone(report["usage"]["cache_write_tokens"])
        self.assertNotIn("PRIVATE_THINKING", json.dumps(report))
        self.assertNotIn("EXPECTED_CODE", json.dumps(report))
        self.assertNotIn("synthetic request", json.dumps(report))
        self.assertGreater(report["prompt_bytes"], report["request_bytes"])
        self.assertEqual(opener.requests[0][0].get_header("Accept"), "text/event-stream")

    def test_buffered_burst_does_not_count_as_live_streaming(self):
        result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(success_stream("CODE")),
            clock=clock([0, 0.01, 1.0, 1.001, 1.002, 1.003]))
        self.assertFalse(result.metadata["streamed_before_completion"])

    def test_empty_deltas_and_keepalives_are_not_content(self):
        data = event("response.output_text.delta", {"delta": ""}) + event("response.completed", completed("CODE"))
        result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(data), clock=clock([0, 1, 2, 3]))
        self.assertIsNone(result.metadata["first_content_s"])
        self.assertFalse(result.metadata["streamed_before_completion"])

    def test_completion_text_is_checked_independently_of_streamed_delta(self):
        data = event("response.output_text.delta", {"delta": "RIGHT"}) + event("response.completed", completed("WRONG"))
        result = probe.stream_turn(NEW, self.body, "RIGHT", opener=FakeOpener(data), clock=clock([0, 1, 2, 3]))
        self.assertTrue(result.metadata["completed"])
        self.assertFalse(result.metadata["output_matches"])

    def test_missing_completed_event_is_not_success(self):
        data = event("response.output_text.delta", {"delta": "CODE"}) + b"data: [DONE]\n\n"
        result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(data), clock=clock([0, 1, 2]))
        self.assertFalse(result.metadata["completed"])
        self.assertEqual(result.metadata["error_type"], "missing_completed_event")

    def test_failure_receipts_never_contain_upstream_error_text(self):
        upstream = event("response.failed", {"error": {"message": "PRIVATE_CREDENTIAL_OR_OUTPUT"}})
        exceptions = [None, ValueError("PRIVATE_CREDENTIAL_OR_OUTPUT"),
                      urllib.error.HTTPError(NEW, 503, "PRIVATE_CREDENTIAL_OR_OUTPUT", {}, io.BytesIO(b"secret"))]
        for exception in exceptions:
            with self.subTest(error=type(exception).__name__):
                result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(upstream, error=exception))
                self.assertFalse(result.metadata["completed"])
                self.assertNotIn("PRIVATE_CREDENTIAL", json.dumps(result.metadata))
                self.assertIsNotNone(result.metadata["error_type"])

    def test_non_sse_and_truncated_or_malformed_frames_fail_safely(self):
        cases = [(b'{"PRIVATE":"OUTPUT"}', "application/json"),
                 (b"event: response.completed\ndata: {}", "text/event-stream"),
                 (b"data: PRIVATE_OUTPUT\n\n", "text/event-stream")]
        for data, content_type in cases:
            result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(data, content_type))
            self.assertFalse(result.metadata["completed"])
            self.assertNotIn("PRIVATE", json.dumps(result.metadata))
            self.assertIsNotNone(result.metadata["duration_s"])

    def test_multiline_sse_json_and_utf8_text_are_supported(self):
        data = 'event: response.output_text.delta\ndata: {\ndata: "delta": "验证"}\n\n'.encode()
        data += event("response.completed", completed("验证"))
        result = probe.stream_turn(NEW, self.body, "验证", opener=FakeOpener(data), clock=clock([0, 1, 2, 3]))
        self.assertTrue(result.metadata["output_matches"])

    def test_absent_or_invalid_cache_usage_is_unknown_not_zero(self):
        for usage in (None, {}, {"input_tokens_details": {"cached_tokens": "123"}},
                      {"input_tokens_details": {"cached_tokens": True}},
                      {"input_tokens_details": {"cached_tokens": -1}}):
            payload = completed("CODE")
            payload["response"]["usage"] = usage
            result = probe.stream_turn(NEW, self.body, "CODE", opener=FakeOpener(event("response.completed", payload)))
            self.assertIsNone(result.metadata["usage"]["cache_read_tokens"])


class ComparisonTests(unittest.TestCase):
    def fake_runner(self, *, new_cache=20000, new_duration=3, fail_new=False, omit_usage=False):
        captures = []

        def runner(url, body, code):
            captures.append((url, copy.deepcopy(body), code))
            new = url == probe.endpoint(NEW)
            cache = new_cache if new else 2000
            duration = new_duration if new else 10
            output = "PRIVATE_WRONG_OUTPUT" if new and fail_new else code
            payload = completed(output, cache=cache, reasoning=new)
            if omit_usage:
                payload["response"].pop("usage")
            data = (event("response.created", {}) + event("response.reasoning_summary_text.delta", {"delta": "PRIVATE_THINKING"})
                    + event("response.output_text.delta", {"delta": output}) + event("response.completed", payload))
            return probe.stream_turn(url, body, code, opener=FakeOpener(data),
                clock=clock([0, 0.1, 0.2, duration - 0.2, duration - 0.1, duration]))

        return runner, captures

    def test_full_comparison_uses_real_parser_and_appends_each_returned_output(self):
        runner, captures = self.fake_runner()
        receipt = probe.compare(LEGACY, NEW, rows=2, turns=3, runner=runner, run_id="fixed-run-id")
        self.assertEqual(len(captures), 6)
        self.assertEqual([value[0] for value in captures], [probe.endpoint(LEGACY), probe.endpoint(NEW),
            probe.endpoint(NEW), probe.endpoint(LEGACY), probe.endpoint(LEGACY), probe.endpoint(NEW)])
        self.assertEqual(captures[0][1]["input"], captures[1][1]["input"])
        self.assertEqual(captures[0][2], captures[1][2])
        # Native output's reasoning is retained in subsequent request history,
        # while no model output survives into the evidence receipt.
        self.assertTrue(any(item.get("type") == "reasoning" for item in captures[2][1]["input"]))
        self.assertTrue(any(item.get("role") == "assistant" for item in captures[3][1]["input"]))
        self.assertTrue(all("previous_response_id" not in body for _, body, _ in captures))
        self.assertEqual(receipt["summary"]["outcome"], "demonstrated")
        serialized = json.dumps(receipt)
        self.assertNotIn("PRIVATE_THINKING", serialized)
        self.assertNotIn("CB_CHECK_", serialized)
        self.assertNotIn("summary=stable", serialized)
        self.assertEqual(receipt["summary"]["followup_medians"]["new"]["followup_cache_read_tokens"], 20000)

    def test_regression_and_unknown_usage_are_explicitly_not_demonstrated(self):
        for options in ({"new_cache": 2000}, {"new_duration": 11}, {"omit_usage": True}):
            runner, _ = self.fake_runner(**options)
            receipt = probe.compare(LEGACY, NEW, rows=1, turns=3, runner=runner)
            self.assertEqual(receipt["summary"]["outcome"], "not_demonstrated")

    def test_failure_stops_only_failed_mode_and_is_not_hidden_from_summary(self):
        runner, captures = self.fake_runner(fail_new=True)
        receipt = probe.compare(LEGACY, NEW, rows=1, turns=3, runner=runner)
        self.assertEqual(len(captures), 4)
        self.assertFalse(receipt["summary"]["all_turns_completed_and_matched"])
        self.assertEqual(receipt["summary"]["outcome"], "not_demonstrated")
        self.assertNotIn("PRIVATE_WRONG_OUTPUT", json.dumps(receipt))

    def test_one_turn_cannot_demonstrate_continuation_cache_improvement(self):
        runner, _ = self.fake_runner()
        receipt = probe.compare(LEGACY, NEW, rows=1, turns=1, runner=runner)
        self.assertEqual(receipt["summary"]["outcome"], "not_demonstrated")

    def test_fresh_run_id_prevents_response_ledger_replay_between_experiments(self):
        runner, captures = self.fake_runner()
        first = probe.compare(LEGACY, NEW, rows=1, turns=1, runner=runner)
        second = probe.compare(LEGACY, NEW, rows=1, turns=1, runner=runner)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(captures[0][1]["input"], captures[2][1]["input"])
        self.assertEqual(first["corpus_sha256"], second["corpus_sha256"])


class CliTests(unittest.TestCase):
    def test_dry_run_estimates_twenty_to_forty_thousand_initial_tokens_without_network_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            with patch.object(probe, "compare", side_effect=AssertionError("no network")), patch("sys.stdout", new_callable=io.StringIO) as out:
                status = probe.main(["--legacy-url", LEGACY, "--new-url", NEW, "--output", str(path)])
            self.assertEqual(status, 0)
            self.assertFalse(path.exists())
            cost = json.loads(out.getvalue())
            self.assertFalse(cost["live"])
            self.assertEqual(cost["sdk_requests_max"], 6)
            self.assertGreaterEqual(cost["estimated_first_turn_input_tokens"]["min"], 20000)
            self.assertLessEqual(cost["estimated_first_turn_input_tokens"]["max"], 40000)

    def test_live_writes_sanitized_receipt_and_returns_failure_for_unproven_improvement(self):
        runner, _ = ComparisonTests().fake_runner(new_duration=12)
        receipt = probe.compare(LEGACY, NEW, rows=1, turns=2, runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "receipt.json"
            with patch.object(probe, "compare", return_value=receipt) as compare, patch("sys.stdout", new_callable=io.StringIO):
                status = probe.main(["--legacy-url", LEGACY, "--new-url", NEW, "--output", str(path), "--live"])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(path.read_text())["summary"]["outcome"], "not_demonstrated")
            compare.assert_called_once()

    def test_endpoints_reject_credentials_queries_external_hosts_and_same_target(self):
        for url in ("http://user:secret@127.0.0.1:1234", "http://127.0.0.1:1234?key=secret",
                    "https://127.0.0.1:1234", "http://external.example:1234", "http://127.0.0.1:1234/other"):
            with self.assertRaises(ValueError):
                probe.endpoint(url)
        with self.assertRaises(ValueError):
            probe.compare(LEGACY, LEGACY, rows=1)
        self.assertEqual(probe.endpoint("http://[::1]:1234/v1/"), "http://[::1]:1234/v1/responses")

    def test_bad_sizes_fail_before_any_inference(self):
        for rows, turns in ((0, 3), (1, 0)):
            with self.assertRaises(ValueError):
                probe.estimate(rows, turns)


if __name__ == "__main__":
    unittest.main()
