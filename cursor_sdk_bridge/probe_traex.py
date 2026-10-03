"""Small synthetic TraeX acceptance probes; never print generated text or tool data."""
import argparse
import base64
import json
import re
import secrets

from cursor_sdk_bridge import traex
from cursor_sdk_bridge.probe_service import ProbeFailed


def post(port, payload):
    try:
        result = traex.request('/v1/responses', port, data=json.dumps(payload).encode(), timeout=1800)
    except Exception as exc:
        raise ProbeFailed('TraeX preflight failed (' + type(exc).__name__ + ').') from None
    if result.get('status') != 'completed':
        raise ProbeFailed('TraeX preflight did not complete.')
    return result


def output_text(result):
    return ''.join(part.get('text', '') for item in result.get('output', []) if item.get('type') == 'message'
                   for part in item.get('content', []) if part.get('type') == 'output_text').strip()


def probe(port=traex.PORT):
    marker, returned = 'PROBE_' + secrets.token_hex(8), 'RESULT_' + secrets.token_hex(8)
    history = [{'role': 'user', 'content': f'Call bridge_probe.echo with value {marker}. After the tool result, reply with only the returned value.'}]
    tools = [{'type': 'namespace', 'name': 'bridge_probe', 'tools': [{'type': 'function', 'name': 'echo',
        'description': 'Synthetic echo', 'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}},
        'required': ['value'], 'additionalProperties': False}}]}]
    base = {'model': traex.DEFAULT_MODEL, 'reasoning': {'effort': 'low'}, 'max_output_tokens': 512,
            'input': history, 'tools': tools, 'store': False, 'stream': False}
    first = post(port, {**base, 'tool_choice': {'type': 'function', 'namespace': 'bridge_probe', 'name': 'echo'}})
    calls = [i for i in first.get('output', []) if i.get('type') == 'function_call']
    if len(calls) != 1:
        raise ProbeFailed('TraeX preflight expected one synthetic tool call.')
    call = calls[0]
    if (call.get('namespace') != 'bridge_probe' or call.get('name') != 'echo'
            or json.loads(call.get('arguments', '{}')) != {'value': marker}
            or call.get('id') == call.get('call_id')):
        raise ProbeFailed('TraeX preflight tool identity or arguments mismatch.')
    history.extend(first['output'])
    history.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': returned})
    last = post(port, {**base, 'tool_choice': 'none'})
    if output_text(last) != returned:
        raise ProbeFailed('TraeX preflight did not consume the tool result.')
    return {'provider': 'traex', 'model': traex.DEFAULT_MODEL, 'model_outputs': 2, 'namespace_roundtrip': True,
            'usage': [first.get('usage'), last.get('usage')]}


def probe_image(port=traex.PORT):
    from cursor_sdk_bridge.probe_service import PALETTE, quadrant_png
    colors = secrets.SystemRandom().sample(sorted(PALETTE), 4)
    image = {'type': 'input_image', 'detail': 'high',
             'image_url': 'data:image/png;base64,' + base64.b64encode(quadrant_png(colors, 256)).decode()}
    prompt = ('Name the colors of the four quadrants in order top-left, top-right, bottom-left, bottom-right. '
              'Choose from red, green, blue, yellow, black, white. Reply only four color words.')
    result = post(port, {'model': traex.DEFAULT_MODEL, 'reasoning': {'effort': 'low'}, 'max_output_tokens': 128,
                        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}, image]}]})
    words = re.findall(r'[a-z]+', output_text(result).lower())
    if words != colors:
        raise ProbeFailed('TraeX image preflight did not identify the four synthetic colors.')
    return {'provider': 'traex', 'model_outputs': 1, 'image_input': True, 'usage': result.get('usage')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=traex.PORT)
    parser.add_argument('--image', action='store_true')
    args = parser.parse_args()
    try:
        traex.verify_service(args.port)
        print(json.dumps(probe_image(args.port) if args.image else probe(args.port)))
    except ProbeFailed as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
