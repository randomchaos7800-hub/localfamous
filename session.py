"""Session persistence — SQLite conversation history.

Schema:
  sessions(id, persona, created_at, updated_at)
  messages(id, session_id, role, content, tool_data, timestamp)

No ORM. Raw sqlite3.
"""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class Session:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    persona TEXT NOT NULL,
                    external_id TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_data TEXT,
                    timestamp TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, id);
            """)
            # Migrate existing databases that lack the external_id column
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "external_id" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN external_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_external ON sessions(external_id)"
            )

    def create(self, persona: str, session_id: str | None = None) -> str:
        """Create a new session. Returns session_id."""
        sid = session_id or str(uuid.uuid4())[:8]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, persona) VALUES (?, ?)",
                (sid, persona),
            )
        return sid

    def get_or_create(self, persona: str, external_id: str) -> str:
        """Look up a session by external_id (e.g. Slack thread), create if missing."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE external_id = ?",
                (external_id,),
            ).fetchone()
            if row:
                return row["id"]
            sid = str(uuid.uuid4())[:8]
            conn.execute(
                "INSERT INTO sessions (id, persona, external_id) VALUES (?, ?, ?)",
                (sid, persona, external_id),
            )
        return sid

    def add_message(self, session_id: str, role: str, content: str | None, tool_data: dict | None = None) -> None:
        """Append a message to a session."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_data) VALUES (?, ?, ?, ?)",
                (session_id, role, content, json.dumps(tool_data) if tool_data else None),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )

    def add_messages_bulk(self, session_id: str, messages: list[dict]) -> None:
        """Add multiple messages from a normalized message list."""
        with self._connect() as conn:
            for msg in messages:
                role = msg["role"]
                content = msg.get("content")
                tool_data = None
                if msg.get("tool_calls"):
                    tool_data = {"tool_calls": msg["tool_calls"]}
                if msg.get("tool_call_id"):
                    tool_data = {
                        "tool_call_id": msg["tool_call_id"],
                        "name": msg.get("name", ""),
                    }
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, tool_data) VALUES (?, ?, ?, ?)",
                    (session_id, role, content, json.dumps(tool_data) if tool_data else None),
                )
            conn.execute(
                "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )

    def get_history(self, session_id: str) -> list[dict]:
        """Load full message history for a session as normalized dicts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_data FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

        result = []
        for row in rows:
            msg: dict[str, Any] = {"role": row["role"]}
            if row["content"] is not None:
                msg["content"] = row["content"]
            if row["tool_data"]:
                td = json.loads(row["tool_data"])
                msg.update(td)
            result.append(msg)
        return result

    def list_sessions(self, persona: str | None = None) -> list[dict]:
        """List all sessions, optionally filtered by persona."""
        with self._connect() as conn:
            if persona:
                rows = conn.execute(
                    "SELECT id, persona, created_at, updated_at FROM sessions "
                    "WHERE persona = ? ORDER BY updated_at DESC",
                    (persona,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, persona, created_at, updated_at FROM sessions "
                    "ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_session_info(self, session_id: str) -> dict | None:
        """Get session metadata."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, persona, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def message_count(self, session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row["n"] if row else 0

    def export_jsonl(self, session_id: str, path: str | Path) -> None:
        """Export a session to JSONL format."""
        history = self.get_history(session_id)
        info = self.get_session_info(session_id)
        with open(path, "w", encoding="utf-8") as f:
            if info:
                f.write(json.dumps({"type": "session_meta", **info}) + "\n")
            for msg in history:
                f.write(json.dumps(msg) + "\n")

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its messages."""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def get_nth_from_last_message_id(self, session_id: str, n: int) -> int | None:
        """Return the ID of the nth-from-last message (boundary for compaction)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        return rows[-1]["id"] if len(rows) == n else None

    def replace_with_summary(self, session_id: str, summary: str, boundary_id: int) -> None:
        """
        Delete all messages before boundary_id, then reuse the first deleted row's
        ID for the summary so it sorts before the kept messages.
        """
        summary_content = f"[CONVERSATION SUMMARY — earlier context]\n{summary}"
        with self._connect() as conn:
            # Find the lowest message id in the session (will become the summary slot)
            first_row = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not first_row:
                return
            first_id = first_row["id"]

            # Delete everything before boundary_id
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND id < ?",
                (session_id, boundary_id),
            )
            # Insert summary at the original first_id position so it sorts first
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                (first_id, session_id, "user", summary_content),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )
