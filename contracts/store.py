"""SQLite persistence for contract violations and session drift metrics."""

from __future__ import annotations
import sqlite3
import logging
from pathlib import Path

log = logging.getLogger("frank.contracts.store")


class ViolationStore:
    """Persists contract violations and session drift metrics to SQLite."""

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS violations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    persona TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    clause_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    description TEXT,
                    timestamp TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS session_metrics (
                    session_id TEXT PRIMARY KEY,
                    persona TEXT NOT NULL,
                    total_calls INTEGER DEFAULT 0,
                    hard_violations INTEGER DEFAULT 0,
                    soft_violations INTEGER DEFAULT 0,
                    recovered INTEGER DEFAULT 0,
                    alpha REAL DEFAULT 0.0,
                    gamma REAL DEFAULT 0.0,
                    drift REAL DEFAULT 0.0,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_violations_session
                    ON violations(session_id);
                CREATE INDEX IF NOT EXISTS idx_violations_persona
                    ON violations(persona, timestamp);
            """)

    def record(
        self,
        session_id: str,
        persona: str,
        tool_name: str,
        clause_id: str,
        severity: str,
        description: str,
    ):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO violations (session_id, persona, tool_name, clause_id, severity, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, persona, tool_name, clause_id, severity, description),
            )

    def update_metrics(
        self,
        session_id: str,
        persona: str,
        total_calls: int,
        hard_violations: int,
        soft_violations: int,
        recovered: int,
        alpha: float,
        gamma: float,
        drift: float,
    ):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO session_metrics
                       (session_id, persona, total_calls, hard_violations, soft_violations,
                        recovered, alpha, gamma, drift, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(session_id) DO UPDATE SET
                       total_calls=excluded.total_calls,
                       hard_violations=excluded.hard_violations,
                       soft_violations=excluded.soft_violations,
                       recovered=excluded.recovered,
                       alpha=excluded.alpha,
                       gamma=excluded.gamma,
                       drift=excluded.drift,
                       updated_at=excluded.updated_at""",
                (session_id, persona, total_calls, hard_violations, soft_violations,
                 recovered, alpha, gamma, drift),
            )

    def get_session_stats(self, session_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_metrics WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            stats = dict(row)
            violations = conn.execute(
                "SELECT clause_id, severity, tool_name, description, timestamp "
                "FROM violations WHERE session_id = ? ORDER BY timestamp DESC LIMIT 20",
                (session_id,),
            ).fetchall()
            stats["violations"] = [dict(v) for v in violations]
            return stats

    def get_persona_summary(self, persona: str, days: int = 7) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                       COUNT(DISTINCT session_id) as sessions,
                       SUM(total_calls) as total_calls,
                       SUM(hard_violations) as hard_violations,
                       SUM(soft_violations) as soft_violations,
                       AVG(drift) as avg_drift,
                       MAX(drift) as max_drift
                   FROM session_metrics
                   WHERE persona = ?
                     AND updated_at >= datetime('now', ? || ' days')""",
                (persona, f"-{days}"),
            ).fetchone()
            if not row or row["sessions"] is None:
                return {"persona": persona, "sessions": 0}
            result = dict(row)
            result["persona"] = persona
            result["days"] = days
            recent = conn.execute(
                """SELECT clause_id, severity, tool_name, description, timestamp
                   FROM violations
                   WHERE persona = ?
                     AND timestamp >= datetime('now', ? || ' days')
                   ORDER BY timestamp DESC LIMIT 10""",
                (persona, f"-{days}"),
            ).fetchall()
            result["recent_violations"] = [dict(v) for v in recent]
            return result

    def list_sessions(self, persona: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if persona:
                rows = conn.execute(
                    "SELECT * FROM session_metrics WHERE persona = ? ORDER BY updated_at DESC LIMIT 10",
                    (persona,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM session_metrics ORDER BY updated_at DESC LIMIT 20",
                ).fetchall()
            return [dict(r) for r in rows]
