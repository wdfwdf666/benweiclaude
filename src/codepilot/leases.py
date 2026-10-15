"""Cross-process single-writer leases for durable conversation threads."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class ThreadBusyError(RuntimeError):
    """Raised when another process currently owns a thread's write lease."""


class LeaseLostError(ThreadBusyError):
    """Raised when a former owner tries to write with a stale fencing token."""


class ThreadLeaseManager:
    """Coordinate one active writer per thread across processes.

    SQLite transactions protect lease acquisition, while a heartbeat keeps a
    long model/tool turn alive. Local RLocks make nested operations such as
    send() -> compact() re-entrant without releasing the database lease early.
    """

    def __init__(self, db_path: Path, *, ttl_seconds: float = 120.0,
                 heartbeat_seconds: float = 20.0) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_id = uuid4().hex
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.heartbeat_seconds = max(
            0.25, min(float(heartbeat_seconds), self.ttl_seconds / 3),
        )
        self.connection = sqlite3.connect(
            db_path, check_same_thread=False, timeout=30, isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self._db_lock = threading.RLock()
        self._locks_guard = threading.Lock()
        self._local_locks: dict[str, threading.RLock] = {}
        self._depth = threading.local()
        self._heartbeat_stops: dict[str, threading.Event] = {}
        self._tokens: dict[str, int] = {}
        with self._db_lock:
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS codepilot_thread_leases (
                    thread_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    fence_token INTEGER NOT NULL DEFAULT 1
                )"""
            )
            columns = {
                row["name"] for row in self.connection.execute(
                    "PRAGMA table_info(codepilot_thread_leases)"
                ).fetchall()
            }
            if "fence_token" not in columns:
                self.connection.execute(
                    "ALTER TABLE codepilot_thread_leases "
                    "ADD COLUMN fence_token INTEGER NOT NULL DEFAULT 1"
                )

    def _local_lock(self, thread_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._local_locks.setdefault(thread_id, threading.RLock())

    def _depths(self) -> dict[str, int]:
        depths = getattr(self._depth, "values", None)
        if depths is None:
            depths = {}
            self._depth.values = depths
        return depths

    def acquire(self, thread_id: str) -> int:
        now = time.time()
        expires = now + self.ttl_seconds
        with self._db_lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    """INSERT INTO codepilot_thread_leases
                       (thread_id, owner_id, acquired_at, expires_at, fence_token)
                       VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(thread_id) DO UPDATE SET
                           owner_id = excluded.owner_id,
                           acquired_at = excluded.acquired_at,
                           expires_at = excluded.expires_at,
                           fence_token = CASE
                               WHEN codepilot_thread_leases.owner_id = excluded.owner_id
                               THEN codepilot_thread_leases.fence_token
                               ELSE codepilot_thread_leases.fence_token + 1
                           END
                       WHERE codepilot_thread_leases.owner_id = excluded.owner_id
                          OR codepilot_thread_leases.expires_at <= ?""",
                    (thread_id, self.owner_id, now, expires, now),
                )
                if cursor.rowcount != 1:
                    row = self.connection.execute(
                        "SELECT owner_id, expires_at FROM codepilot_thread_leases WHERE thread_id = ?",
                        (thread_id,),
                    ).fetchone()
                    self.connection.rollback()
                    remaining = max(0.0, float(row["expires_at"]) - now) if row else 0.0
                    owner = str(row["owner_id"])[:8] if row else "unknown"
                    raise ThreadBusyError(
                        f"Thread {thread_id[:12]} is being modified by another process "
                        f"(owner {owner}, lease expires in about {remaining:.0f}s)"
                    )
                row = self.connection.execute(
                    "SELECT fence_token FROM codepilot_thread_leases WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                token = int(row["fence_token"])
                self.connection.commit()
                self._tokens[thread_id] = token
                return token
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def _heartbeat(self, thread_id: str, token: int, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            now = time.time()
            with self._db_lock:
                cursor = self.connection.execute(
                    """UPDATE codepilot_thread_leases SET expires_at = ?
                       WHERE thread_id = ? AND owner_id = ? AND fence_token = ?""",
                    (now + self.ttl_seconds, thread_id, self.owner_id, token),
                )
            if cursor.rowcount != 1:
                return

    def _start_heartbeat(self, thread_id: str, token: int) -> None:
        stop = threading.Event()
        self._heartbeat_stops[thread_id] = stop
        threading.Thread(
            target=self._heartbeat, args=(thread_id, token, stop), daemon=True,
            name=f"codepilot-lease-{thread_id[:8]}",
        ).start()

    def release(self, thread_id: str) -> None:
        stop = self._heartbeat_stops.pop(thread_id, None)
        if stop is not None:
            stop.set()
        token = self._tokens.pop(thread_id, None)
        with self._db_lock:
            self.connection.execute(
                """DELETE FROM codepilot_thread_leases
                   WHERE thread_id = ? AND owner_id = ? AND fence_token = ?""",
                (thread_id, self.owner_id, token),
            )

    def assert_current(self, thread_id: str, token: int | None = None) -> int:
        """Verify this process still owns the current lease generation."""
        expected = token if token is not None else self._tokens.get(thread_id)
        now = time.time()
        with self._db_lock:
            row = self.connection.execute(
                """SELECT owner_id, fence_token, expires_at
                   FROM codepilot_thread_leases WHERE thread_id = ?""",
                (thread_id,),
            ).fetchone()
        if (expected is None or row is None or row["owner_id"] != self.owner_id
                or int(row["fence_token"]) != expected or float(row["expires_at"]) <= now):
            raise LeaseLostError(
                f"Thread {thread_id[:12]} lease generation is stale; stop this write"
            )
        return expected

    @contextmanager
    def lease(self, thread_id: str):
        local_lock = self._local_lock(thread_id)
        with local_lock:
            depths = self._depths()
            depth = depths.get(thread_id, 0)
            if depth == 0:
                token = self.acquire(thread_id)
                self._start_heartbeat(thread_id, token)
            else:
                token = self.assert_current(thread_id)
            depths[thread_id] = depth + 1
            try:
                yield token
            finally:
                remaining = depths[thread_id] - 1
                if remaining:
                    depths[thread_id] = remaining
                else:
                    depths.pop(thread_id, None)
                    self.release(thread_id)

    def status(self, thread_id: str) -> dict | None:
        now = time.time()
        with self._db_lock:
            row = self.connection.execute(
                """SELECT owner_id, acquired_at, expires_at, fence_token
                   FROM codepilot_thread_leases WHERE thread_id = ? AND expires_at > ?""",
                (thread_id, now),
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_id": row["owner_id"],
            "owned_by_this_process": row["owner_id"] == self.owner_id,
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "fence_token": row["fence_token"],
        }

    def close(self) -> None:
        for stop in list(self._heartbeat_stops.values()):
            stop.set()
        with self._db_lock:
            self.connection.execute(
                "DELETE FROM codepilot_thread_leases WHERE owner_id = ?", (self.owner_id,),
            )
            self.connection.close()
