import logging
from pathlib import Path
from datetime import datetime
import sqlite3


log = logging.getLogger("offrers.storage")

DB_PATH = Path("data.db")


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied (
            vacancy_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evaluated (
            vacancy_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            score INTEGER,
            reason TEXT,
            evaluated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    log.debug("db connection opened: %s", db_path)
    return conn


def has_applied(conn: sqlite3.Connection, vacancy_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM applied WHERE vacancy_id = ?", (vacancy_id,)
    ).fetchone()
    return row is not None


def get_evaluation(conn: sqlite3.Connection, vacancy_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM evaluated WHERE vacancy_id = ?", (vacancy_id,)
    ).fetchone()


def mark_evaluated(
    conn: sqlite3.Connection,
    vacancy_id: str,
    status: str,
    score: int | None,
    reason: str,
) -> None:
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO evaluated (vacancy_id, status, score, reason, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (vacancy_id, status, score, reason, now),
    )
    conn.commit()
    log.debug("evaluated %s -> %s (score=%s)", vacancy_id, status, score)


def mark_applied(conn: sqlite3.Connection, vacancy_id: str) -> None:
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO applied (vacancy_id, applied_at) VALUES (?, ?)",
        (vacancy_id, now),
    )
    conn.commit()
    log.debug("applied recorded: %s", vacancy_id)


def mark_failed(conn: sqlite3.Connection, vacancy_id: str, reason: str) -> None:
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO evaluated (vacancy_id, status, score, reason, evaluated_at) "
        "VALUES (?, 'failed', NULL, ?, ?)",
        (vacancy_id, reason, now),
    )
    conn.commit()
    log.debug("failed recorded: %s (%s)", vacancy_id, reason)


def count_today(conn: sqlite3.Connection) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM applied WHERE applied_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return row["cnt"] if row else 0


def get_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) as cnt FROM applied").fetchone()["cnt"]
    today = count_today(conn)
    return {"total": total, "today": today}
