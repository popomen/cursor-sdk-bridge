"""All SDK-advertised Opus 5.5 combinations must survive each public boundary."""
import itertools
import json
from pathlib import Path
import unittest

from cursor_sdk_bridge.anthropic_protocol import prepare_messages
from cursor_sdk_bridge.models import MODELS, claude_model
from cursor_sdk_bridge.responses_protocol import InvalidRequest, prepare_request


VARIANTS = [
    (f"claude-opus-5-5-{effort}" + ("-300k" if context == "300k" else "") + ("-fast" if fast else ""),
     context, effort, fast)
    for context, effort, fast in itertools.product(
        ("300k", "1m"), ("low", "medium", "high", "xhigh", "max"), (False, True))
]


def expected_selection(context, effort, fast):
    return {"id": "claude-opus-5-5", "params": [
        {"id": "context", "value": context}, {"id": "effort", "value": effort},
        {"id": "fast", "value": str(fast).lower()}]}


class ModelTests(unittest.TestCase):
    def test_all_twenty_variants_have_distinct_sdk_parameters_and_correct_client_windows(self):
        self.assertEqual(set(MODELS), {row[0] for row in VARIANTS})
        catalog = json.loads((Path(__file__).parents[1] / 'cursor_sdk_bridge/assets/models.json').read_text())['models']
        self.assertEqual([row['slug'] for row in catalog], list(MODELS))
        by_id = {row['slug']: row for row in catalog}
        for alias, context, effort, fast in VARIANTS:
            with self.subTest(model=alias):
                self.assertEqual(MODELS[alias].selection(), expected_selection(context, effort, fast))
                self.assertEqual(by_id[alias]['default_reasoning_level'], effort)
                self.assertEqual(by_id[alias]['supported_reasoning_levels'], [{'effort': effort, 'description': effort}])
                self.assertEqual(by_id[alias]['context_window'], 1000000 if context == '1m' else 300000)
                self.assertEqual(claude_model(alias), alias + ('[1m]' if context == '1m' else ''))

    def test_both_protocols_accept_all_variants_and_responses_reject_mismatched_effort(self):
        for alias, context, effort, fast in VARIANTS:
            with self.subTest(model=alias):
                prepare_request({'model': alias, 'input': 'hello', 'reasoning': {'effort': effort}})
                request, *_ = prepare_messages({'model': alias, 'max_tokens': 128,
                    'messages': [{'role': 'user', 'content': 'hello'}]})
                self.assertEqual(request['model'], alias)
                with self.assertRaises(InvalidRequest):
                    prepare_request({'model': alias, 'input': 'hello',
                                     'reasoning': {'effort': 'low' if effort != 'low' else 'high'}})

    def test_original_ids_keep_their_sdk_semantics(self):
        for effort in ('high', 'xhigh', 'max'):
            self.assertEqual(MODELS['claude-opus-5-5-' + effort].selection(),
                             expected_selection('1m', effort, False))


if __name__ == '__main__':
    unittest.main()
