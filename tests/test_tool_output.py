import copy
import json
import unittest

from jsonschema import ValidationError
from cursor_sdk_bridge.tool_output import parse_output, strict_json


TOOL = {'name': 'lookup', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                                      'required': ['query'], 'additionalProperties': False}}
CALL = {'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': '{"query":"value"}'}


class ToolOutputTests(unittest.TestCase):
    def test_valid_output_preserves_public_call_and_normalizes_arguments(self):
        source = {'output': [CALL, {'type': 'message', 'text': 'done'}]}
        raw, normalized = parse_output(json.dumps(source), [TOOL])
        self.assertEqual(raw, source['output'])
        self.assertEqual(normalized, [{'type': 'function_call', 'name': 'lookup', 'arguments': {'query': 'value'}},
                                      {'type': 'message', 'text': 'done'}])

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for value in ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json(value)

    def test_output_envelope_is_strict(self):
        valid = json.dumps({'output': [CALL]})
        for value in ('```json\n' + valid + '\n```', '{"output":[],"output":[]}', '{"output":[]}',
                      '{"output":{}}', '{"output":[null]}', '{"output":[{"type":"unknown"}]}',
                      '{"output":[{"type":"message","text":1}]}', '{"output":[{"type":"message","text":"ok","extra":1}]}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_output(value, [TOOL])

    def test_calls_reject_unknown_tools_ids_and_invalid_arguments(self):
        invalid = []
        for key, value in (('name', 'unknown'), ('call_id', ''), ('arguments', '{}'), ('arguments', {}),
                           ('arguments', '{"query":1}'), ('arguments', '{"query":"a","query":"b"}'),
                           ('arguments', '{"query":"a","extra":1}')):
            call = copy.deepcopy(CALL)
            call[key] = value
            invalid.append({'output': [call]})
        invalid.extend([{'output': [CALL, CALL]}, {'output': [{**CALL, 'extra': 1}]}])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, ValidationError)):
                parse_output(json.dumps(value), [TOOL])


if __name__ == '__main__':
    unittest.main()
