"""Three-provider extension to the existing recoverable configuration transaction."""
import fcntl
import json
import os
import tomllib

from cursor_sdk_bridge import switch_config as sc, traex


def switch_multi(directory, mode, port, prepared=None):
    import tomlkit
    if mode not in ('openai', 'cursor', 'traex'):
        raise ValueError('Unknown provider')
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Codex directory must be a real directory')
    state_dir = directory / 'cursor-fallback-state'
    if state_dir.is_symlink():
        raise ValueError('State directory cannot be a symlink')
    state_dir.mkdir(mode=0o700, exist_ok=True)
    lock_path, state_path = state_dir / 'switch.lock', state_dir / 'state.json'
    sc.read(lock_path)
    pack = lambda value: json.dumps(value, ensure_ascii=False, indent=2).encode()
    with lock_path.open('a+b') as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_bytes = sc.read(state_path)
        state = json.loads(state_bytes) if state_bytes else None
        if state and state.get('version') not in (2, 3):
            raise ValueError('Recover legacy state with openai first')
        if state and state.get('transaction'):
            if state['version'] != 3:
                raise ValueError('Recover the existing two-provider transaction first')
            transaction = state['transaction']
            current = sc.read(directory / 'config.toml')
            if current not in (sc.decode(transaction['config.toml']['before']), sc.decode(transaction['config.toml']['after'])):
                raise ValueError('Interrupted switch conflicts with external configuration')
            if prepared:
                final = dict(state)
                final.pop('transaction')
                prepared({'config': sc.decode(transaction['config.toml']['after']),
                          'state': None if transaction['mode'] == 'openai' else pack(final)}, state_bytes)
            state = sc.finish_transaction(directory, state_path, state, expected_state=state_bytes)
            state_bytes = sc.read(state_path)
        config = sc.read(directory / 'config.toml')
        document = tomlkit.parse((config or b'').decode())
        provider = document.get('model_provider', 'openai')
        if state:
            if not sc.matches_installed(document, state):
                raise ValueError('Managed provider configuration changed externally')
            original = sc.decode(state['original_config'])
        else:
            if provider != 'openai':
                raise ValueError('No OpenAI baseline for current provider')
            original = config
        if provider == mode and not (mode == 'cursor' and sc.catalog_path_upgrade_required(document)):
            return
        baseline = tomlkit.parse((original or b'').decode())
        if mode == 'openai':
            for path in sc.MANAGED:
                source = sc.get(baseline, path)
                if source['present']:
                    value = baseline
                    for part in path:
                        value = value[part]
                    source['value'] = value
                sc.assign(document, path, source)
            sc.retain_cursor_provider(document, baseline, state)
            for parent in ('features', 'model_providers'):
                if parent not in baseline and parent in document and not document[parent]:
                    del document[parent]
        else:
            catalog = traex.verify_service(port) if mode == 'traex' else None
            if mode == 'cursor':
                sc.verify_service(port)
            values = {
                'model_provider': mode,
                'model': traex.DEFAULT_MODEL if mode == 'traex' else 'claude-opus-5-5-high',
                'model_reasoning_effort': 'medium' if mode == 'traex' else 'high',
                'web_search': 'disabled', 'features.code_mode': False, 'features.code_mode_only': False,
                'features.enable_request_compression': False,
                'model_providers.' + mode: traex.provider(port) if mode == 'traex' else {
                    'name': 'Cursor SDK', 'base_url': f'http://127.0.0.1:{port}/v1',
                    'wire_api': 'responses', 'supports_websockets': False, 'requires_openai_auth': False},
            }
            if provider == mode:
                values['model'] = document.get('model', values['model'])
                values['model_reasoning_effort'] = document.get('model_reasoning_effort', values['model_reasoning_effort'])
            if mode == 'cursor':
                values['model_catalog_json'] = str(sc.bundled_catalog())
            else:
                values['model_catalog_json'] = str(traex.catalog_snapshot(directory, catalog))
            for path in sc.MANAGED:
                name = '.'.join(path)
                if path[0] == 'model_providers' and path[1] != mode:
                    continue
                sc.assign(document, path, {'present': True, 'value': values[name]} if name in values else {'present': False})
            state = {**(state or {}), 'version': 3, 'original_config': sc.encode(original), 'installed': sc.managed(document)}
            if catalog:
                state['traex_models'] = {m['slug']: [e['effort'] for e in m['supported_reasoning_levels']] for m in catalog}
        new_config = tomlkit.dumps(document).encode()
        tomllib.loads(new_config.decode())
        if sc.read(directory / 'config.toml') != config or sc.read(state_path) != state_bytes:
            raise ValueError('Configuration changed during preparation')
        state = {**state, 'transaction': {'mode': mode, 'config.toml': {
            'before': sc.encode(config), 'after': sc.encode(new_config)}}}
        staged = pack(state)
        if prepared:
            final = dict(state)
            final.pop('transaction')
            prepared({'config': new_config, 'state': None if mode == 'openai' else pack(final)}, staged)
        if sc.read(directory / 'config.toml') != config or sc.read(state_path) != state_bytes:
            raise ValueError('Configuration changed during preparation')
        sc.atomic(state_path, staged)
        sc.finish_transaction(directory, state_path, state, expected_state=staged)
