"""One command for the bridge services, client configuration and releases."""
import argparse
import json
import os
from pathlib import Path
import sys
import tomllib

from cursor_bridge import version


def invoke(module, arguments):
    previous = sys.argv
    try:
        sys.argv = ['cursor-bridge'] + list(arguments)
        return module.main()
    finally:
        sys.argv = previous


def status():
    from cursor_bridge import dashboard, switch_config
    from cursor_bridge.appserver_runtime import DesktopRuntime
    codex_dir = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    result = dashboard.Dashboard().snapshot()
    result.update(running_version=version.running_version(), deployed_version=version.deployed_version())
    try:
        codex = switch_config.status(codex_dir)
        runtime = DesktopRuntime(codex_dir)
        observed = runtime.inspect()
        config = tomllib.loads((switch_config.read(codex_dir / 'config.toml') or b'').decode())
        codex.update(runtime=observed, runtime_matches_config=runtime.matches(observed, config))
        result['codex'] = codex
    except Exception as exc:
        result['codex'] = {'state': 'unverified', 'error_type': type(exc).__name__}
    return result


def serve(arguments):
    instance = arguments.pop(0) if arguments and arguments[0] in ('codex', 'claude', 'dashboard') else 'codex'
    if instance == 'dashboard':
        from cursor_bridge import dashboard
        return invoke(dashboard, arguments)
    from cursor_bridge import cursor_sdk2api
    defaults = []
    if instance == 'claude':
        defaults = ['--port', '8790', '--state-dir', str(Path.home() / '.codex/cursor-sdk2api-claude'),
                    '--timeout-max', '1800', '--queue-timeout', '1800']
    return invoke(cursor_sdk2api, defaults + arguments)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'serve', 'switch', 'restart', 'probe', 'deploy'))
    if not arguments or arguments[0] in ('-h', '--help'):
        parser.print_help()
        print('\nserve [codex|claude|dashboard] [adapter options]\n'
              'switch codex|claude cursor|restore [switch options]\n'
              'restart codex|claude|dashboard\n'
              'probe [--messages|--image] [--port PORT] [--effort high|xhigh|max]\n'
              'deploy [COMMIT] [--install-only] [--repo PATH]')
        return
    command = parser.parse_args(arguments[:1]).command
    rest = arguments[1:]
    if command == 'serve':
        return serve(rest)
    if command == 'probe':
        from cursor_bridge import probe_service
        if not any(arg in ('-h', '--help') for arg in rest):
            print('Probe uses 1–2 real SDK outputs (usually 1–2 minutes at high; consumes Cursor quota).', file=sys.stderr)
        return invoke(probe_service, rest)
    if command == 'switch':
        switch_parser = argparse.ArgumentParser(prog='cursor-bridge switch')
        switch_parser.add_argument('client', choices=('codex', 'claude'))
        switch_parser.add_argument('mode', choices=('cursor', 'restore'))
        selected, extra = switch_parser.parse_known_args(rest)
        if selected.client == 'codex':
            from cursor_bridge import switch_config as module
            mode = 'openai' if selected.mode == 'restore' else selected.mode
        else:
            from cursor_bridge import claude_switch as module
            mode = selected.mode
        if mode == 'cursor' and not any(arg in ('-h', '--help') for arg in extra):
            print('Switch preflight may use 2 real SDK outputs (usually 1–2 minutes at high).', file=sys.stderr)
        return invoke(module, [mode] + extra)
    if command == 'status':
        parser.parse_args(arguments)
        print(json.dumps(status(), ensure_ascii=False))
        return
    from cursor_bridge import deployment
    if command == 'restart':
        restart_parser = argparse.ArgumentParser(prog='cursor-bridge restart')
        restart_parser.add_argument('instance', choices=deployment.INSTANCES)
        selected = restart_parser.parse_args(rest)
        from cursor_bridge import dashboard
        if selected.instance == 'dashboard':
            error = dashboard.run_restart(deployment.unit_name('dashboard'))
            result = {'instance': 'dashboard', 'result': 'failed' if error else 'restarted', 'message': error}
        else:
            result = dashboard.Dashboard().restart(selected.instance)
        print(json.dumps(result, ensure_ascii=False))
        if result['result'] != 'restarted':
            raise SystemExit(1)
        return
    deploy_parser = argparse.ArgumentParser(prog='cursor-bridge deploy')
    deploy_parser.add_argument('commit', nargs='?', default='HEAD')
    deploy_parser.add_argument('--repo', type=Path, default=Path.cwd())
    deploy_parser.add_argument('--root', type=Path)
    deploy_parser.add_argument('--install-only', action='store_true',
                               help='only install the release; leave current, launchers and user units unchanged')
    selected = deploy_parser.parse_args(rest)
    try:
        result = deployment.deploy(**vars(selected))
    except deployment.DeploymentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))
