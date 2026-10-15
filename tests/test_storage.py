import sqlite3

from codepilot.storage import SessionStore


def test_existing_session_database_gets_permission_mode_column(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """CREATE TABLE codepilot_sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "INSERT INTO codepilot_sessions VALUES ('old', 'Old', 'qwen', 't1', 't2')"
    )
    connection.commit()
    connection.close()

    store = SessionStore(db)
    assert store.get("old")["permission_mode"] == "default"
    store.close()
