"""Local file snapshots independent of LangGraph conversation checkpoints."""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .permissions import TRACKED_FILE_EDITS

MAX_SNAPSHOT_BYTES = 2_000_000


class FileCheckpoints:
    def __init__(self, db_path: Path, workspace: Path, resolve_path) -> None:
        # Keep this connection separate from SqliteSaver's transaction scope.
        self.connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.workspace = workspace
        self.resolve_path = resolve_path
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS codepilot_file_checkpoints (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                changes_json TEXT NOT NULL
            )"""
        )
        self.connection.commit()

    def affected_paths(self, tool_name: str, args: dict) -> list[Path]:
        if tool_name not in TRACKED_FILE_EDITS:
            return []
        names = ("source", "destination") if tool_name in {"move_file", "copy_file"} else ("path",)
        return list(dict.fromkeys(self.resolve_path(args[name]) for name in names))

    @staticmethod
    def _read(path: Path) -> str | None:
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"Cannot snapshot non-file path: {path}")
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"File is too large for a reversible edit ({MAX_SNAPSHOT_BYTES} bytes): {path}")
        return base64.b64encode(path.read_bytes()).decode("ascii")

    def capture(self, paths: list[Path]) -> dict[str, str | None]:
        return {str(path.relative_to(self.workspace)): self._read(path) for path in paths}

    def restore_snapshot(self, snapshot: dict[str, str | None]) -> None:
        for name, encoded in snapshot.items():
            path = self.resolve_path(name)
            if encoded is None:
                if path.is_file():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(encoded))

    def record(
        self, session_id: str, tool_name: str,
        before: dict[str, str | None], after: dict[str, str | None],
    ) -> str | None:
        changes = [
            {"path": path, "before": old, "after": after[path]}
            for path, old in before.items() if old != after[path]
        ]
        if not changes:
            return None
        checkpoint_id = uuid4().hex
        self.connection.execute(
            """INSERT INTO codepilot_file_checkpoints
               (id, session_id, created_at, tool_name, changes_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                checkpoint_id, session_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                tool_name, json.dumps(changes),
            ),
        )
        self.connection.commit()
        return checkpoint_id

    def list(self, session_id: str) -> list[dict]:
        rows = self.connection.execute(
            """SELECT seq, id, created_at, tool_name, changes_json
               FROM codepilot_file_checkpoints WHERE session_id = ? ORDER BY seq DESC""",
            (session_id,),
        ).fetchall()
        return [
            {"seq": row["seq"], "id": row["id"], "created_at": row["created_at"],
             "tool_name": row["tool_name"],
             "paths": [change["path"] for change in json.loads(row["changes_json"])]}
            for row in rows
        ]

    def rewind(self, session_id: str, checkpoint_prefix: str) -> list[str]:
        matching = [item for item in self.list(session_id) if item["id"].startswith(checkpoint_prefix)]
        if len(matching) != 1:
            raise ValueError("Checkpoint ID not found or prefix is ambiguous")
        first_seq = matching[0]["seq"]
        rows = self.connection.execute(
            """SELECT changes_json FROM codepilot_file_checkpoints
               WHERE session_id = ? AND seq >= ? ORDER BY seq DESC""",
            (session_id, first_seq),
        ).fetchall()
        expected: dict[str, str | None] = {}
        target: dict[str, str | None] = {}
        for row in rows:
            for change in json.loads(row["changes_json"]):
                path = change["path"]
                expected.setdefault(path, change["after"])
                target[path] = change["before"]

        # Refuse to overwrite edits made by the user or another process later.
        paths = {name: self.resolve_path(name) for name in expected}
        for name, path in paths.items():
            if self._read(path) != expected[name]:
                raise ValueError(f"Cannot rewind: {name} changed since the checkpoint")

        try:
            self.restore_snapshot(target)
        except Exception:
            self.restore_snapshot(expected)
            raise

        self.connection.execute(
            "DELETE FROM codepilot_file_checkpoints WHERE session_id = ? AND seq >= ?",
            (session_id, first_seq),
        )
        self.connection.commit()
        return list(target)

    def close(self) -> None:
        self.connection.close()
