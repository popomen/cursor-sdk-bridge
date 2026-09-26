#!/usr/bin/env python3
"""Bounded live readiness checks: two SDK outputs each, synthetic tools and images only."""
import argparse
import base64
import json
import secrets
import struct
import urllib.error
import urllib.request
import zlib


# Default service SDK deadlines plus queue margin; data keepalives do not end a blocking POST.
CLIENT_TIMEOUTS = {'high': 1235, 'xhigh': 1235, 'max': 1235}


class ProbeFailed(RuntimeError):
    pass


def probe(port=8789, effort='high'):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    marker = 'FALLBACK_PROBE_OK_' + secrets.token_hex(8)
    result_marker = 'FALLBACK_RESULT_' + secrets.token_hex(8)
    history = [{'role': 'user', 'content': f'Call fallback_probe.echo with value "{marker}". '
                'After receiving its result, reply with exactly that returned text and nothing else.'}]
    tool = {'type': 'namespace', 'name': 'fallback_probe', 'description': 'Synthetic readiness check',
            'tools': [{'type': 'function', 'name': 'echo', 'description': 'Accepts a verification value and returns a fresh result marker',
                       'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}},
                                      'required': ['value'], 'additionalProperties': False}}]}

    def post(choice):
        body = {'model': 'claude-opus-5-5-' + effort, 'input': history, 'tools': [tool],
                'tool_choice': choice, 'stream': False, 'store': False}
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/responses',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        try:
            with opener.open(request, timeout=CLIENT_TIMEOUTS[effort]) as response:
                value = json.load(response)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise ProbeFailed('Cursor live preflight failed (' + type(exc).__name__ + '); configuration was not switched.') from None
        if value.get('status') != 'completed':
            raise ProbeFailed('Cursor live preflight did not complete; configuration was not switched.')
        return value['output']

    first = post({'type': 'function', 'namespace': 'fallback_probe', 'name': 'echo'})
    calls = [item for item in first if item.get('type') == 'function_call']
    if len(calls) != 1 or calls[0].get('namespace') != 'fallback_probe' or calls[0].get('name') != 'echo':
        raise ProbeFailed('Cursor live preflight returned an unexpected tool; nothing was executed.')
    call = calls[0]
    if json.loads(call['arguments']) != {'value': marker}:
        raise ProbeFailed('Cursor live preflight returned incorrect synthetic arguments.')
    history.extend(first)
    history.append({'type': 'function_call_output', 'call_id': call['call_id'], 'output': result_marker})
    last = post('none')
    text = ''.join(part.get('text', '') for item in last if item.get('type') == 'message'
                   for part in item.get('content', []) if part.get('type') == 'output_text')
    if text.strip() != result_marker:
        raise ProbeFailed('Cursor live preflight did not consume the synthetic tool result correctly.')
    return {'effort': effort, 'sdk_outputs': 2, 'namespace_roundtrip': True}


def probe_messages(port=8790, effort='high'):
    """Anthropic Messages round trip: a forced synthetic tool_use, then text built from its tool_result."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    marker = 'FALLBACK_PROBE_OK_' + secrets.token_hex(8)
    result_marker = 'FALLBACK_RESULT_' + secrets.token_hex(8)
    tool = {'name': 'fallback_probe_echo', 'description': 'Accepts a verification value and returns a fresh result marker',
            'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}},
                             'required': ['value'], 'additionalProperties': False}}
    messages = [{'role': 'user', 'content': f'Call fallback_probe_echo with value "{marker}". '
                 'After receiving its result, reply with exactly that returned text and nothing else.'}]

    def post(choice):
        body = {'model': 'claude-opus-5-5-' + effort, 'max_tokens': 1024, 'messages': messages, 'tools': [tool],
                'tool_choice': choice, 'stream': False}
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/messages', data=json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'})
        try:
            with opener.open(request, timeout=CLIENT_TIMEOUTS[effort]) as response:
                value = json.load(response)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise ProbeFailed('Cursor Messages probe failed (' + type(exc).__name__ + '); settings were not switched.') from None
        if value.get('type') != 'message':
            raise ProbeFailed('Cursor Messages probe returned no message; settings were not switched.')
        return value

    first = post({'type': 'tool', 'name': 'fallback_probe_echo'})
    calls = [block for block in first.get('content', []) if block.get('type') == 'tool_use']
    if first.get('stop_reason') != 'tool_use' or len(calls) != 1 or calls[0].get('name') != 'fallback_probe_echo':
        raise ProbeFailed('Cursor Messages probe returned an unexpected tool; nothing was executed.')
    if calls[0].get('input') != {'value': marker}:
        raise ProbeFailed('Cursor Messages probe returned incorrect synthetic arguments.')
    messages.append({'role': 'assistant', 'content': first['content']})
    messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': calls[0]['id'],
                                                   'content': result_marker}]})
    last = post({'type': 'none'})
    text = ''.join(block.get('text', '') for block in last.get('content', []) if block.get('type') == 'text')
    if last.get('stop_reason') != 'end_turn' or text.strip() != result_marker:
        raise ProbeFailed('Cursor Messages probe did not consume the synthetic tool result correctly.')
    return {'api': 'messages', 'effort': effort, 'sdk_outputs': 2, 'tool_roundtrip': True}


IMAGE_SOURCES = ('user_message', 'function_call_output')
PALETTE = {'red': (220, 30, 30), 'green': (30, 170, 60), 'blue': (30, 60, 220), 'yellow': (240, 220, 30),
           'black': (0, 0, 0), 'white': (255, 255, 255)}
VIEW_IMAGE = {'type': 'function', 'name': 'view_image', 'description': 'Synthetic image viewer; never executed',
              'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                             'required': ['path'], 'additionalProperties': False}}


def quadrant_png(colors, size=64):
    """RGB PNG with solid quadrants in top-left, top-right, bottom-left, bottom-right order."""
    half = size // 2
    rows = b''.join(bytes(1) + bytes(PALETTE[colors[0 if y < half else 2]]) * half
                    + bytes(PALETTE[colors[1 if y < half else 3]]) * half for y in range(size))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    header = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    return (bytes.fromhex('89504e470d0a1a0a') + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(rows))
            + chunk(b'IEND', b''))


def probe_image(port=8789, effort='high', sources=IMAGE_SOURCES):
    """One SDK output per source: the model must read back a random four-color PNG exactly."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    question = ('Name the colors of the four quadrants of the attached image in the order top-left, top-right, '
                'bottom-left, bottom-right, using only these words: ' + ', '.join(PALETTE) + '. '
                'Reply with exactly four lowercase words separated by single spaces and nothing else.')
    for source in sources:
        colors = secrets.SystemRandom().sample(sorted(PALETTE), 4)
        image = {'type': 'input_image', 'detail': 'auto',
                 'image_url': 'data:image/png;base64,' + base64.b64encode(quadrant_png(colors)).decode()}
        if source == 'user_message':
            history = [{'role': 'user', 'content': [{'type': 'input_text', 'text': question}, image]}]
        else:
            call_id = 'view_' + secrets.token_hex(8)
            history = [{'role': 'user', 'content': 'Open /tmp/fallback-probe.png with view_image. ' + question},
                       {'type': 'function_call', 'call_id': call_id, 'name': 'view_image',
                        'arguments': json.dumps({'path': '/tmp/fallback-probe.png'})},
                       {'type': 'function_call_output', 'call_id': call_id, 'output': [image]}]
        body = {'model': 'claude-opus-5-5-' + effort, 'input': history, 'tools': [VIEW_IMAGE],
                'tool_choice': 'none', 'stream': False, 'store': False}
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/responses',
                    data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        try:
            with opener.open(request, timeout=CLIENT_TIMEOUTS[effort]) as response:
                value = json.load(response)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise ProbeFailed('Cursor image probe failed (' + type(exc).__name__ + ') for ' + source + '.') from None
        text = ''.join(part.get('text', '') for item in value.get('output', []) if item.get('type') == 'message'
                       for part in item.get('content', []) if part.get('type') == 'output_text')
        if value.get('status') != 'completed' or text.strip().lower().split() != colors:
            raise ProbeFailed('Cursor image probe did not read the synthetic image correctly (' + source + ').')
    return {'effort': effort, 'sdk_outputs': len(sources), 'image_sources': list(sources)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8789)
    parser.add_argument('--effort', choices=('high', 'xhigh', 'max'), default='high')
    parser.add_argument('--image', action='store_true',
                        help='check image input instead: a random four-color PNG, one SDK output per source')
    parser.add_argument('--image-source', choices=IMAGE_SOURCES + ('both',), default='both')
    parser.add_argument('--messages', action='store_true',
                        help='check Anthropic Messages instead: a synthetic tool round trip, two SDK outputs '
                             '(use --port 8790 for the Claude Code instance)')
    args = parser.parse_args()
    try:
        if args.messages:
            print(json.dumps(probe_messages(args.port, args.effort)))
        elif args.image:
            sources = IMAGE_SOURCES if args.image_source == 'both' else (args.image_source,)
            print(json.dumps(probe_image(args.port, args.effort, sources)))
        else:
            print(json.dumps(probe(args.port, args.effort)))
    except Exception as exc:
        print(str(exc) if isinstance(exc, ProbeFailed) else 'Preflight failed: ' + type(exc).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
