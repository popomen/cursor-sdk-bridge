import json
import unittest

from cursor_bridge.request_log import REQUEST_STATS
from cursor_bridge.responses_protocol import complete_response, recover_output, response_shell
from cursor_bridge.tool_output import strict_json

MODEL = "claude-opus-5-5-xhigh"
NL, FENCE = chr(10), "`" * 3
TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {
    "query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}
CALL = {"type": "function_call", "call_id": "call_new", "name": "lookup", "arguments": json.dumps({"query": "x"})}
NOTE = "Checking the latest failure first."
OUTPUT = json.dumps({"output": [CALL]})


def recorded(text, body):
    stats = {}
    token = REQUEST_STATS.set(stats)
    try:
        response = complete_response(response_shell(MODEL), text, body, [])
    finally:
        REQUEST_STATS.reset(token)
    return response, stats


class RecoverOutputTests(unittest.TestCase):
    def test_bare_json_is_unchanged(self):
        self.assertEqual(recover_output(OUTPUT), (OUTPUT, None))

    def test_single_fence_is_unwrapped(self):
        body, repair = recover_output(FENCE + "json" + NL + OUTPUT + NL + FENCE)
        self.assertEqual((strict_json(body), repair), (strict_json(OUTPUT), "code_fence"))

    def test_leading_note_becomes_the_first_message(self):
        for text in (NOTE + NL + NL + OUTPUT, NOTE + NL + NL + FENCE + "json" + NL + OUTPUT + NL + FENCE + NL):
            with self.subTest(text=text[-8:]):
                body, repair = recover_output(text)
                self.assertEqual(repair, "prose_prefix")
                self.assertEqual(strict_json(body), {"output": [{"type": "message", "text": NOTE}, CALL]})

    def test_note_repeating_an_existing_message_is_not_duplicated(self):
        value = {"output": [{"type": "message", "text": NOTE}, CALL]}
        body, repair = recover_output(NOTE.replace(" ", "  ") + NL + NL + json.dumps(value))
        self.assertEqual((strict_json(body), repair), (value, "prose_prefix"))

    def test_braces_inside_the_note_are_skipped(self):
        note = "The config has {key: value} and " + json.dumps({"a": 1}) + " in it."
        body, _ = recover_output(note + NL + OUTPUT)
        self.assertEqual(strict_json(body)["output"], [{"type": "message", "text": note}, CALL])

    def test_other_malformed_outputs_are_left_invalid(self):
        key = json.dumps("output")
        duplicate = "{" + key + ": [], " + key + ": [" + json.dumps(CALL) + "]}"
        extra = json.dumps({"output": [], "extra": 1})
        for text in ("Only a note.", OUTPUT + NL + "Trailing text.", NOTE + NL + OUTPUT[:-2],
                     NOTE + NL + extra, NOTE + NL + duplicate):
            with self.subTest(text=text[:40]):
                body, repair = recover_output(text)
                self.assertIsNone(repair)
                with self.assertRaises(ValueError):
                    strict_json(body)


class CompleteResponseTests(unittest.TestCase):
    def test_recovered_note_passes_schema_checks_and_is_recorded(self):
        body = {"model": MODEL, "input": "x", "tools": [TOOL], "tool_choice": "required"}
        response, stats = recorded(NOTE + NL + OUTPUT, body)
        self.assertEqual([item["type"] for item in response["output"]], ["message", "function_call"])
        self.assertEqual(response["output"][0]["content"][0]["text"], NOTE)
        self.assertEqual(response["output"][1]["name"], "lookup")
        self.assertEqual(stats, {"output_repair": "prose_prefix"})

    def test_bare_output_records_no_repair(self):
        _, stats = recorded(OUTPUT, {"model": MODEL, "input": "x", "tools": [TOOL]})
        self.assertEqual(stats, {})

    def test_recovered_output_still_obeys_the_schema(self):
        bad = json.dumps({"output": [dict(CALL, arguments=json.dumps({"query": 1}))]})
        with self.assertRaises(Exception):
            recorded(NOTE + NL + bad, {"model": MODEL, "input": "x", "tools": [TOOL]})


if __name__ == "__main__":
    unittest.main()
