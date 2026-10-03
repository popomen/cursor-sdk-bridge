"""Install immutable Git releases and register persistent, initially stopped user units."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from cursor_sdk_bridge import version

INSTANCES = ('codex', 'claude', 'traex', 'dashboard')
MANAGED = '# Managed by cursor-sdk-bridge.\n'


class DeploymentError(RuntimeError):
    pass


def command(args, **kwargs):
    """Capture subprocess output: dependency installers may contain credential-bearing URLs."""
    try:
        return subprocess.run([str(arg) for arg in args], check=True, capture_output=True,
                              text=True, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError('Command failed: %s (%s)' % (Path(str(args[0])).name, type(exc).__name__)) from None


def atomic_write(path, data, mode=0o644):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_link(path, target):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + next(tempfile._get_candidate_names()))
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unit_name(instance):
    if instance not in INSTANCES:
        raise DeploymentError('Unknown instance')
    return 'cursor-sdk-bridge-%s.service' % instance


def quote_unit(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise DeploymentError('A unit path contains a newline')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def service_path():
    # Keep Node and the user's proxychains wrapper available without copying shell environment values.
    paths = [str(Path.home() / '.local/bin')]
    for executable in ('node', 'proxychains4'):
        found = shutil.which(executable)
        if found:
            paths.append(str(Path(found).parent))
    return ':'.join(dict.fromkeys(paths + ['/usr/local/bin', '/usr/bin', '/bin']))


def unit_text(instance, root, release=None):
    current = Path(release) if release is not None else (Path(root) / 'current').resolve()
    if not current.is_absolute() or any(char in str(current) for char in ('\n', '\r', '\0')):
        raise DeploymentError('A unit working directory must be an absolute, single-line path')
    # WorkingDirectory is a scalar path, unlike ExecStart's shell-like word list:
    # systemd treats surrounding quotes here as literal path characters.
    working_directory = str(current).replace('%', '%%')
    return (MANAGED + '[Unit]\nDescription=Cursor SDK Bridge ' + instance + '\nAfter=network-online.target\n'
            '\n[Service]\nType=simple\nWorkingDirectory=' + working_directory + '\n'
            'ExecStart=' + quote_unit(current / 'venv/bin/python') + ' -m cursor_sdk_bridge serve ' + instance + '\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n'
            'Environment=' + quote_unit('PATH=' + service_path()) + '\n'
            'Environment=' + quote_unit('CURSOR_SDK_BRIDGE_DEPLOY_ROOT=' + str(root)) + '\n'
            'UnsetEnvironment=PYTHONPATH PYTHONHOME\nUMask=0077\nRestart=on-failure\nRestartSec=3\n'
            'TimeoutStopSec=30\n\n[Install]\nWantedBy=default.target\n')


def extract_commit(repo, commit, destination):
    try:
        result = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', commit],
                                capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError('Git archive failed (%s)' % type(exc).__name__) from None
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                raise DeploymentError('Release archive contains an unsafe entry')
        archive.extractall(destination)


def install_release(repo, commit, root, runner=command, extractor=extract_commit):
    repo, root = Path(repo).resolve(), Path(root).expanduser().resolve()
    resolved = runner(['git', '-C', repo, 'rev-parse', '--verify', '--end-of-options', commit + '^{commit}'],
                      timeout=10).stdout.strip()
    if len(resolved) != 40 or any(c not in '0123456789abcdef' for c in resolved):
        raise DeploymentError('Git did not return a commit SHA')
    release = root / resolved
    if release.exists():
        if version.manifest_version(release / version.MANIFEST) != resolved or not (release / 'venv/bin/python').is_file():
            raise DeploymentError('Incomplete release directory exists; preserve it for inspection')
        return release
    release.mkdir(mode=0o755)
    try:
        extractor(repo, resolved, release)
        if not (release / 'pyproject.toml').is_file() or not (release / 'cursor_sdk_bridge/cli.py').is_file():
            raise DeploymentError('Commit is not a cursor-sdk-bridge release')
        traex_source = release / 'integrations/traex'
        if (traex_source / 'package-lock.json').is_file():
            runner(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=traex_source, timeout=600)
            runner(['npm', 'run', 'build:bridge'], cwd=traex_source, timeout=120)
            if not (release / 'cursor_sdk_bridge/assets/traex/server.mjs').is_file():
                raise DeploymentError('TraeX build did not produce its service bundle')
        runner([sys.executable, '-m', 'venv', release / 'venv'], timeout=120)
        python = release / 'venv/bin/python'
        runner([python, '-m', 'pip', 'install', '--disable-pip-version-check', release], timeout=600)
        runner([python, '-m', 'pip', 'check'], timeout=60)
        frozen = runner([python, '-m', 'pip', 'freeze', '--all'], timeout=60).stdout
        # The inventory belongs to the private release; no package-index URLs are logged or committed.
        atomic_write(release / 'installed-dependencies.txt', frozen.encode(), 0o600)
        atomic_write(release / version.MANIFEST, json.dumps({'commit': resolved, 'created_at': time.time()},
                                                         sort_keys=True).encode())
        return release
    except BaseException:
        shutil.rmtree(release)
        raise


def deploy(commit='HEAD', repo='.', root=None, unit_dir=None, bin_dir=None, install_only=False, runner=command,
           extractor=extract_commit):
    root = Path(root or version.deploy_root()).expanduser().resolve()
    unit_dir = Path(unit_dir or Path.home() / '.config/systemd/user').expanduser()
    bin_dir = Path(bin_dir or Path.home() / '.local/bin').expanduser()
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.deploy.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        release = install_release(repo, commit, root, runner, extractor)
        result = {'commit': release.name, 'release': str(release), 'installed': True, 'selected': False,
                  'units_enabled': False, 'services_restarted': False}
        if install_only:
            return result
        current = root / 'current'
        if current.exists() and not current.is_symlink():
            raise DeploymentError('current exists and is not a managed symlink')
        previous = os.readlink(current) if current.is_symlink() else None
        launcher = bin_dir / 'cursor-sdk-bridge'
        launcher_target = root / 'current/venv/bin/cursor-sdk-bridge'
        if launcher.exists() or launcher.is_symlink():
            if not launcher.is_symlink() or os.readlink(launcher) != str(launcher_target):
                raise DeploymentError('Existing cursor-sdk-bridge command belongs to another installation')
        unit_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        units = {unit_dir / unit_name(name): unit_text(name, root, release).encode() for name in INSTANCES}
        before = {}
        for path in units:
            if path.is_symlink():
                raise DeploymentError('Refusing to replace a unit symlink')
            data = path.read_bytes() if path.exists() else None
            if data is not None and not data.startswith(MANAGED.encode()):
                raise DeploymentError('Existing user unit is not managed by cursor-sdk-bridge')
            before[path] = data
        launcher_existed = launcher.is_symlink()
        wants = {unit_dir / 'default.target.wants' / path.name: path for path in units}
        enabled_before = {path for path in wants if path.exists() or path.is_symlink()}
        try:
            for path, data in units.items():
                atomic_write(path, data)
            runner(['systemd-analyze', '--user', 'verify', *units], timeout=30)
            atomic_link(current, release.name)
            atomic_link(launcher, str(launcher_target))
            runner(['systemctl', '--user', 'daemon-reload'], timeout=30)
            runner(['systemctl', '--user', 'enable', *[unit_name(name) for name in INSTANCES]], timeout=30)
        except BaseException:
            if previous is None:
                current.unlink(missing_ok=True)
            else:
                atomic_link(current, previous)
            for path, data in before.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data)
            if not launcher_existed:
                launcher.unlink(missing_ok=True)
            for link, unit in wants.items():
                if link not in enabled_before and link.is_symlink() and link.resolve() == unit.resolve():
                    link.unlink()
            with contextlib.suppress(Exception):
                runner(['systemctl', '--user', 'daemon-reload'], timeout=30)
            raise
        return {**result, 'selected': True, 'units_enabled': True,
                'next_step': 'Validate the release, then restart each idle instance explicitly.'}


def admin_request(port, action):
    path = {'drain': 'drain', 'resume': 'resume', 'force-drain': 'drain?force=1'}.get(action)
    if path is None:
        raise DeploymentError('Unknown administration action')
    headers = {'Content-Type': 'application/json', 'Connection': 'close'}
    from cursor_sdk_bridge import traex
    if port == traex.PORT:
        headers['Authorization'] = 'Bearer ' + traex.KEY_FILE.read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:%d/admin/%s' % (port, path), data=b'{}',
                                     headers=headers, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code != 409:
                raise DeploymentError('Drain endpoint unavailable (HTTP %d)' % exc.code) from None
            value = json.load(exc)
        finally:
            exc.close()
    except (OSError, ValueError) as exc:
        raise DeploymentError('Drain endpoint unavailable (%s)' % type(exc).__name__) from None
    if not isinstance(value, dict):
        raise DeploymentError('Invalid drain response')
    return value
