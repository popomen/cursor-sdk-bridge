"""Release identity is fixed when a process first asks for it."""
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess

MANIFEST = '.cursor-sdk-bridge-release.json'


def deploy_root():
    return Path(os.environ.get('CURSOR_SDK_BRIDGE_DEPLOY_ROOT', Path.home() / '.local/share/cursor-sdk-bridge')).expanduser()


def manifest_version(path):
    try:
        value = json.loads(Path(path).read_text())['commit']
        return value if isinstance(value, str) and len(value) == 40 and all(c in '0123456789abcdef' for c in value) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


@lru_cache(maxsize=1)
def running_version():
    for directory in Path(__file__).resolve().parents:
        value = manifest_version(directory / MANIFEST)
        if value:
            return value
        if (directory / '.git').exists():
            try:
                result = subprocess.run(['git', '-C', str(directory), 'rev-parse', 'HEAD'],
                                        capture_output=True, text=True, check=True, timeout=3)
                return result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return 'development-unknown'
    return 'unknown'


def deployed_version(root=None):
    return manifest_version(Path(root or deploy_root()) / 'current' / MANIFEST)
