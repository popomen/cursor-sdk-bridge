"""TraeX service launcher and read-only, credential-safe service checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request

from cursor_sdk_bridge import version

PORT = 8792
MODELS = ('GPT-6-Astra', 'GPT-6-Astra[1m]')
DEFAULT_MODEL = MODELS[1]
STATE_DIR = Path.home() / '.codex/traex-bridge'
KEY_FILE = Path.home() / '.traex-bridge/api-key'


def open_request(path, port=PORT, *, data=None, timeout=5):
    # Credentials are read at request time, never printed or put on a command line.
    key = KEY_FILE.read_text().strip()
    if not key or '\n' in key or '\r' in key:
        raise ValueError('Invalid TraeX API key file')
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Connection': 'close'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def request(path, port=PORT, *, data=None, timeout=5):
    with open_request(path, port, data=data, timeout=timeout) as response:
        return json.load(response)


def verify_service(port=PORT):
    from cursor_sdk_bridge.switch_config import ServiceNotReady
    try:
        health = request('/health', port)
        if health.get('service') != 'traex-bridge' or health.get('status') != 'ok' or health.get('draining'):
            raise ValueError('Service not ready')
        required = {'responses', 'namespace_functions', 'image_inputs', 'request_progress', 'drain'}
        if not required.issubset(health.get('capabilities', [])):
            raise ValueError('Missing capabilities')
        models = request('/v1/models', port)['data']
        ids = {m['id'] for m in models}
        if DEFAULT_MODEL not in ids or not ids.issubset(MODELS):
            raise ValueError('Invalid models')
        catalog = request('/models?client_version=0.155.1', port)['models']
        if {m['slug'] for m in catalog} != ids:
            raise ValueError('Invalid Codex catalog')
        return catalog
    except Exception as exc:
        raise ServiceNotReady('TraeX readiness check failed (' + type(exc).__name__ + '); configuration was not switched.') from None


def provider(port=PORT):
    return {'name': 'TraeX Astra', 'base_url': f'http://127.0.0.1:{port}/v1', 'wire_api': 'responses',
            'supports_websockets': False, 'request_max_retries': 0, 'stream_max_retries': 0,
            'stream_idle_timeout_ms': 3600000,
            'auth': {'command': '/bin/cat', 'args': [str(KEY_FILE.resolve())], 'timeout_ms': 1000}}


def catalog_snapshot(directory, models):
    """Immutable metadata asset: rollback only restores the reference in config.toml."""
    from cursor_sdk_bridge.switch_config import atomic, read
    data = json.dumps({'models': models}, ensure_ascii=False, sort_keys=True).encode()
    catalog_dir = directory / 'cursor-fallback-state/traex-catalogs'
    if catalog_dir.is_symlink():
        raise ValueError('Catalog directory cannot be a symlink')
    catalog_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = catalog_dir / (hashlib.sha256(data).hexdigest() + '.json')
    previous = read(path)
    if previous is None:
        atomic(path, data)
    elif previous != data:
        raise ValueError('Immutable catalog changed externally')
    return path.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('invalid port')
    bundle = Path(__file__).resolve().parent / 'assets/traex/server.mjs'
    node = shutil.which('node')
    if not node or not bundle.is_file():
        raise SystemExit('TraeX bundle or Node missing; install a built release first.')
    environment = dict(os.environ)
    # Never inherit upstream debugging flags or alternate listeners into the managed service.
    for key in ('TRAEX_BRIDGE_DUMP_BODY', 'TRAEX_BRIDGE_TRACE', 'TRAEX_BRIDGE_LOG_REQUESTS',
                'TRAEX_BRIDGE_HOST', 'TRAEX_BRIDGE_PORT', 'TRAEX_BRIDGE_API_KEY'):
        environment.pop(key, None)
    environment.update(TRAEX_BRIDGE_ADDR=f'127.0.0.1:{args.port}',
        TRAEX_BRIDGE_STATE_DIR=str(KEY_FILE.parent), TRAEX_BRIDGE_RUNTIME_DIR=str(args.state_dir.resolve()),
        TRAEX_BRIDGE_DEFAULT_MODEL=DEFAULT_MODEL, TRAEX_BRIDGE_LOG_LEVEL='silent',
        CURSOR_SDK_BRIDGE_RUNNING_VERSION=version.running_version())
    os.execve(node, [node, str(bundle)], environment)


if __name__ == '__main__':
    main()
