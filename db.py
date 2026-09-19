import os
import sqlite3
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def get_default_db_path() -> Path:
    """Return configured database path from DATABASE_PATH or default to data/app.db."""
    env_path = os.getenv("DATABASE_PATH")
    if env_path:
        return Path(env_path).resolve()
    return Path(__file__).resolve().parent / "data" / "app.db"


DEFAULT_DB_PATH = get_default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
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
    """Initialize database tables according to the spec schema and apply migrations."""
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        # Check if user_id column exists in sessions table for existing databases
        cursor = conn.execute("PRAGMA table_info(sessions);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "user_id" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;")


def create_user(
    username: str,
    password_hash: str,
    db_path: str | Path | None = None,
) -> int | None:
    """Create a new user. Returns user id or None if username already exists."""
    with get_connection(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?);",
                (username.strip(), password_hash),
            )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None


def get_user_by_username(
    username: str,
    db_path: str | Path | None = None,
) -> sqlite3.Row | None:
    """Retrieve a user by username (case-insensitive)."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ? COLLATE NOCASE;",
            (username.strip(),),
        )
        return cursor.fetchone()


def get_user_by_id(
    user_id: int,
    db_path: str | Path | None = None,
) -> sqlite3.Row | None:
    """Retrieve a user by id."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE id = ?;",
            (user_id,),
        )
        return cursor.fetchone()


def create_session(
    idea: str,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Create a new session with an initial idea and optional user owner."""
    with get_connection(db_path) as conn:
        if user_id is not None:
            cursor = conn.execute(
                "INSERT INTO sessions (idea, status, user_id) VALUES (?, 'asking', ?);",
                (idea, user_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO sessions (idea, status) VALUES (?, 'asking');",
                (idea,),
            )
        return int(cursor.lastrowid)


def get_session(
    session_id: int,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> sqlite3.Row | None:
    """Retrieve a session by its ID, optionally enforcing user ownership."""
    with get_connection(db_path) as conn:
        if user_id is not None:
            cursor = conn.execute(
                "SELECT id, user_id, idea, type, status, created_at FROM sessions WHERE id = ? AND user_id = ?;",
                (session_id, user_id),
            )
        else:
            cursor = conn.execute(
                "SELECT id, user_id, idea, type, status, created_at FROM sessions WHERE id = ?;",
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


def list_sessions(
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> list[sqlite3.Row]:
    """List all sessions ordered newest first, optionally filtered by user."""
    with get_connection(db_path) as conn:
        if user_id is not None:
            cursor = conn.execute(
                "SELECT id, user_id, idea, type, status, created_at FROM sessions WHERE user_id = ? ORDER BY id DESC;",
                (user_id,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, user_id, idea, type, status, created_at FROM sessions ORDER BY id DESC;",
            )
        return cursor.fetchall()


def delete_session(
    session_id: int,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Delete a session and cascade delete its messages and prompts, optionally checking user ownership."""
    with get_connection(db_path) as conn:
        if user_id is not None:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ?;",
                (session_id, user_id),
            )
        else:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
        return cursor.rowcount > 0
