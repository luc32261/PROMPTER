import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  idea        TEXT NOT NULL,
  type        TEXT,                       -- study | writing | research | other
  status      TEXT NOT NULL DEFAULT 'asking',  -- asking | ready
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role        TEXT NOT NULL,              -- user | assistant
  content     TEXT NOT NULL,              -- assistant content stored as raw JSON string
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  final_prompt TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create a database connection with foreign keys enabled."""
    if db_path is None:
        target = DEFAULT_DB_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(target)
    elif isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(db_path)
    else:
        path_str = db_path
        if path_str != ":memory:":
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path_str)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """Initialize database tables according to the spec schema."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


def create_session(idea: str, db_path: str | Path | None = None) -> int:
    """Create a new session with an initial idea."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (idea, status) VALUES (?, 'asking');",
            (idea,),
        )
        return int(cursor.lastrowid)


def get_session(session_id: int, db_path: str | Path | None = None) -> sqlite3.Row | None:
    """Retrieve a session by its ID."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, idea, type, status, created_at FROM sessions WHERE id = ?;",
            (session_id,),
        )
        return cursor.fetchone()


def update_session(
    session_id: int,
    status: str | None = None,
    session_type: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Update status and/or type of a session."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if session_type is not None:
        clauses.append("type = ?")
        params.append(session_type)

    if not clauses:
        return

    params.append(session_id)
    query = f"UPDATE sessions SET {', '.join(clauses)} WHERE id = ?;"
    with get_connection(db_path) as conn:
        conn.execute(query, tuple(params))


def add_message(
    session_id: int,
    role: str,
    content: str,
    db_path: str | Path | None = None,
) -> int:
    """Record a user or assistant message in the conversation history."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?);",
            (session_id, role, content),
        )
        return int(cursor.lastrowid)


def get_messages(session_id: int, db_path: str | Path | None = None) -> list[sqlite3.Row]:
    """Retrieve all messages for a session in chronological order."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, session_id, role, content, created_at FROM messages WHERE session_id = ? ORDER BY id ASC;",
            (session_id,),
        )
        return cursor.fetchall()


def save_final_prompt(
    session_id: int,
    final_prompt: str,
    db_path: str | Path | None = None,
) -> int:
    """Save a generated final prompt for a session."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO prompts (session_id, final_prompt) VALUES (?, ?);",
            (session_id, final_prompt),
        )
        return int(cursor.lastrowid)


def get_final_prompt(session_id: int, db_path: str | Path | None = None) -> sqlite3.Row | None:
    """Get the latest final prompt generated for a session."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, session_id, final_prompt, created_at FROM prompts WHERE session_id = ? ORDER BY id DESC LIMIT 1;",
            (session_id,),
        )
        return cursor.fetchone()


def get_all_prompts(session_id: int, db_path: str | Path | None = None) -> list[sqlite3.Row]:
    """Get all versions of prompts generated for a session ordered oldest to newest."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, session_id, final_prompt, created_at FROM prompts WHERE session_id = ? ORDER BY id ASC;",
            (session_id,),
        )
        return cursor.fetchall()


def list_sessions(db_path: str | Path | None = None) -> list[sqlite3.Row]:
    """List all sessions ordered newest first."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, idea, type, status, created_at FROM sessions ORDER BY id DESC;",
        )
        return cursor.fetchall()


def delete_session(session_id: int, db_path: str | Path | None = None) -> bool:
    """Delete a session and cascade delete its messages and prompts."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
        return cursor.rowcount > 0
