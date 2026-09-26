"""Private result store for retry deduplication; never a request/body log.

Only hashes, completed responses and sanitized usage metadata are retained. The
database is sensitive recovery state, unlike the metadata-only request log.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


def request_digest(body, history, mode):
    request = {key: value for key, value in body.items()
               if key not in ("stream", "store", "previous_response_id", "input")}
    request.update(input=history, bridge_mode=mode, ledger_schema=1)
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class ResultLedger:
    def __init__(self, path, ttl=3600, max_bytes=256 * 1024 * 1024, clock=time.time):
        self.path, self.ttl, self.max_bytes, self.clock = Path(path), ttl, max_bytes, clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not chmod the existing runtime directory or any existing state.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise PermissionError("ledger must be private")
        finally:
            os.close(fd)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (digest TEXT PRIMARY KEY, "
                        "expires REAL NOT NULL, saved REAL NOT NULL, payload TEXT NOT NULL)")
        self.db.commit()

    def get(self, digest):
        with self.lock, self.db:
            self.db.execute("DELETE FROM results WHERE expires <= ?", (self.clock(),))
            row = self.db.execute("SELECT payload FROM results WHERE digest=?", (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, digest, response, stats):
        # Whitelist backend telemetry: never persist exceptions or SDK objects.
        safe_stats = {key: stats[key] for key in ("usage", "queue_s", "output_repair", "engine", "resumed")
                      if key in stats}
        payload = json.dumps({"response": response, "stats": safe_stats}, ensure_ascii=False)
        with self.lock, self.db:
            now = self.clock()
            self.db.execute("DELETE FROM results WHERE expires <= ?", (now,))
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)",
                            (digest, now + self.ttl, now, payload))
            # Bound retained bytes; a single admissible response still fits the
            # adapter's response limit. Eviction means a later retry is cold.
            while self.db.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM results").fetchone()[0] > self.max_bytes:
                self.db.execute("DELETE FROM results WHERE digest=(SELECT digest FROM results ORDER BY saved LIMIT 1)")

    def close(self):
        with self.lock:
            self.db.close()
