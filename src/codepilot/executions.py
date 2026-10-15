"""Durable idempotency journal for model-requested tool calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _arguments_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ToolExecutionJournal:
    """Record a tool call before side effects and reuse its durable outcome."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS codepilot_tool_executions (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY (session_id, tool_call_id)
            )"""
        )
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_codepilot_tool_executions_session
               ON codepilot_tool_executions(session_id, started_at DESC)"""
        )
        self.connection.commit()

    def begin(self, session_id: str, tool_call_id: str, tool_name: str,
              arguments: dict[str, Any]) -> dict[str, Any]:
        digest = _arguments_hash(tool_name, arguments)
        now = _now()
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO codepilot_tool_executions
               (session_id, tool_call_id, tool_name, arguments_hash, status, started_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            (session_id, tool_call_id, tool_name, digest, now),
        )
        self.connection.commit()
        if cursor.rowcount == 1:
            return {"action": "execute", "status": "running", "result": None}

        row = self.connection.execute(
            """SELECT * FROM codepilot_tool_executions
               WHERE session_id = ? AND tool_call_id = ?""",
            (session_id, tool_call_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Tool execution record disappeared after insert")
        if row["tool_name"] != tool_name or row["arguments_hash"] != digest:
            raise ValueError(
                f"Tool call ID collision: {tool_call_id} was already used with different arguments"
            )
        if row["status"] in {"succeeded", "failed"}:
            return {
                "action": "replay", "status": row["status"],
                "result": row["result"] or "",
            }

        if row["status"] == "running":
            self.connection.execute(
                """UPDATE codepilot_tool_executions
                   SET status = 'uncertain', finished_at = ?
                   WHERE session_id = ? AND tool_call_id = ? AND status = 'running'""",
                (now, session_id, tool_call_id),
            )
            self.connection.commit()
        return {
            "action": "uncertain", "status": "uncertain",
            "result": (
                f"Tool execution {tool_call_id} has an uncertain prior outcome after an interrupted "
                "process. It was not executed again. Inspect external state before requesting a new call."
            ),
        }

    def finish(self, session_id: str, tool_call_id: str, *, succeeded: bool,
               result: str) -> None:
        status = "succeeded" if succeeded else "failed"
        cursor = self.connection.execute(
            """UPDATE codepilot_tool_executions
               SET status = ?, result = ?, finished_at = ?
               WHERE session_id = ? AND tool_call_id = ? AND status = 'running'""",
            (status, result[:64_000], _now(), session_id, tool_call_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(f"Tool execution {tool_call_id} is no longer in running state")

    def list(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT tool_call_id, tool_name, status, result, started_at, finished_at
               FROM codepilot_tool_executions WHERE session_id = ?
               ORDER BY started_at DESC LIMIT ?""",
            (session_id, max(1, min(limit, 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.connection.close()
