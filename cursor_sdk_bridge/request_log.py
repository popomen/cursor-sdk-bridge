"""Private, bounded request metadata log without prompts, credentials or upstream text."""
import contextlib
import contextvars
from datetime import datetime
import json
import os
from pathlib import Path
import threading


REQUEST_STATS = contextvars.ContextVar("request_stats", default=None)


class RequestLog:
    def __init__(self, directory, max_bytes=1024 * 1024, backups=3):
        self.path = Path(directory) / "requests.jsonl"
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.Lock()

    def record(self, **fields):
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), **fields}
        line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        with self.lock:
            with contextlib.suppress(OSError):
                self._write(line)

    def _write(self, line):
        directory = self.path.parent
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise OSError("log directory must not be a symlink")
        os.chmod(directory, 0o700)
        with contextlib.suppress(FileNotFoundError):
            if self.path.lstat().st_size + len(line) > self.max_bytes:
                self._rotate()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line)
        finally:
            os.close(fd)

    def _rotate(self):
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
