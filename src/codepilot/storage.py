"""SQLite checkpoint storage and searchable session metadata."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.checkpointer = SqliteSaver(self.connection)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS codepilot_sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                permission_mode TEXT NOT NULL DEFAULT 'default'
            )"""
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(codepilot_sessions)")}
        if "permission_mode" not in columns:
            self.connection.execute(
                "ALTER TABLE codepilot_sessions ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'default'"
            )
        self.connection.commit()

    def create(self, model: str, title: str = "New session", permission_mode: str = "default") -> str:
        session_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.connection.execute(
            """INSERT INTO codepilot_sessions
               (id, title, model, created_at, updated_at, permission_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, title, model, now, now, permission_mode),
        )
        self.connection.commit()
        return session_id

    def get(self, session_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM codepilot_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM codepilot_sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def touch(
        self, session_id: str, *, model: str | None = None,
        title: str | None = None, permission_mode: str | None = None,
    ) -> None:
        current = self.get(session_id)
        if current is None:
            raise ValueError(f"Unknown session: {session_id}")
        self.connection.execute(
            """UPDATE codepilot_sessions
               SET model = ?, title = ?, permission_mode = ?, updated_at = ? WHERE id = ?""",
            (
                model or current["model"],
                title or current["title"],
                permission_mode or current["permission_mode"],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                session_id,
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
