import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def get_default_db_path() -> Path:
    """Return configured database path from DATABASE_PATH or default to data/app.db."""
    env_path = os.getenv("DATABASE_PATH")
    base_dir = Path(__file__).resolve().parent
    if env_path and env_path.strip():
        p = Path(env_path.strip())
        if not p.is_absolute():
            return (base_dir / p).resolve()
        return p.resolve()
    return base_dir / "data" / "app.db"


DEFAULT_DB_PATH = get_default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'user',        -- 'user' | 'admin'
  is_active       INTEGER NOT NULL DEFAULT 1,          -- 1 (active) | 0 (disabled)
  last_login      TEXT,                                -- ISO timestamp
  session_version INTEGER NOT NULL DEFAULT 0,          -- increments on logout/revoke
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS failed_attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failed_attempts_key_time ON failed_attempts(key, created_at);

CREATE TABLE IF NOT EXISTS llm_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  route      TEXT NOT NULL,
  timestamp  TEXT NOT NULL DEFAULT (datetime('now')),
  success    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user_time ON llm_usage(user_id, timestamp);

CREATE TABLE IF NOT EXISTS admin_audit (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_username  TEXT,
  action          TEXT NOT NULL,
  target_username TEXT,
  details         TEXT,
  timestamp       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_id_desc ON admin_audit(id DESC);
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create a database connection with foreign keys enabled and WAL mode."""
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

    conn = sqlite3.connect(path_str, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    if path_str != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.OperationalError:
            pass
    conn.row_factory = sqlite3.Row
    return conn


def check_production_admin_env() -> None:
    """Refuse to start if ADMIN_USERNAME, ADMIN_PASSWORD, or ADMIN_GATE_PASSWORD is missing in production."""
    env = (os.getenv("FLASK_ENV") or os.getenv("ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if env == "production":
        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
        admin_gate_password = os.getenv("ADMIN_GATE_PASSWORD", "").strip()
        if not admin_username or not admin_password or not admin_gate_password:
            raise RuntimeError(
                "ADMIN_USERNAME, ADMIN_PASSWORD, and ADMIN_GATE_PASSWORD environment variables are required in production."
            )


def check_and_migrate_case_insensitive_usernames(conn: sqlite3.Connection) -> list[str]:
    """Check for existing usernames that differ only by case.

    If there are any, do NOT delete or rename anything. Return them and skip adding the unique index.
    If there are none, add a unique index on lower(username).
    """
    cursor = conn.execute(
        "SELECT lower(username) AS lower_name, COUNT(*) AS cnt FROM users GROUP BY lower(username) HAVING cnt > 1;"
    )
    duplicates = [row["lower_name"] for row in cursor.fetchall()]
    if duplicates:
        return duplicates

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lower_username ON users(lower(username));")
    return []


def sync_admin(db_path: str | Path | None = None) -> None:
    """Synchronize the single backend-defined admin user on startup.

    Order:
    (a) demote any other admin to 'user';
    (b) if no user has ADMIN_USERNAME, create it with role 'admin' and the ADMIN_PASSWORD hash;
        if it exists as a normal user, promote it;
    (c) set the admin's password hash from ADMIN_PASSWORD;
    (d) only then create CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON users(role) WHERE role = 'admin';
    """
    admin_username = os.getenv("ADMIN_USERNAME", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_username or not admin_password:
        return

    from werkzeug.security import generate_password_hash

    with get_connection(db_path) as conn:
        # Demote any other user with role='admin' whose username != admin_username
        # This ensures that even if one_admin index already exists, inserting/promoting
        # the target admin will not violate the unique index on role='admin'.
        conn.execute(
            "UPDATE users SET role = 'user' WHERE role = 'admin' AND username != ? COLLATE NOCASE;",
            (admin_username,),
        )

        cursor = conn.execute(
            "SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE;",
            (admin_username,),
        )
        existing = cursor.fetchone()
        password_hash = generate_password_hash(admin_password)

        if not existing:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, session_version) VALUES (?, ?, 'admin', 1, 0);",
                (admin_username, password_hash),
            )
            admin_user_id = int(cursor.lastrowid)
        else:
            admin_user_id = int(existing["id"])
            conn.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?;",
                (admin_user_id,),
            )

        # (c) set the admin's password hash from ADMIN_PASSWORD
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?;",
            (password_hash, admin_user_id),
        )

        # (d) only then create CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON users(role) WHERE role = 'admin';
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_admin ON users(role) WHERE role = 'admin';"
        )


def init_db(db_path: str | Path | None = None) -> list[str]:
    """Initialize database tables according to the spec schema and apply migrations."""
    check_production_admin_env()
    case_duplicates: list[str] = []
    try:
        with get_connection(db_path) as conn:
            conn.executescript(SCHEMA)

            # Migration: Ensure users table has role, is_active, last_login, session_version
            cursor = conn.execute("PRAGMA table_info(users);")
            user_columns = [row["name"] for row in cursor.fetchall()]
            if "role" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user';")
            if "is_active" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
            if "last_login" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT;")
            if "session_version" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;")
            if "daily_limit" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN daily_limit INTEGER DEFAULT NULL;")

            # Migration: Check if user_id column exists in sessions table for existing databases
            cursor = conn.execute("PRAGMA table_info(sessions);")
            session_columns = [row["name"] for row in cursor.fetchall()]
            if "user_id" not in session_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;")

            # Migration: Case-insensitive usernames unique index check
            case_duplicates = check_and_migrate_case_insensitive_usernames(conn)
    except sqlite3.OperationalError:
        pass
    return case_duplicates


def create_user(
    username: str,
    password_hash: str,
    role: str = "user",
    is_active: int = 1,
    db_path: str | Path | None = None,
) -> int | None:
    """Create a new user. Always assigns role='user'. Returns user id or None if username already exists."""
    with get_connection(db_path) as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, session_version) VALUES (?, ?, 'user', ?, 0);",
                (username.strip(), password_hash, is_active),
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
        try:
            cursor = conn.execute(
                "SELECT id, username, password_hash, role, is_active, last_login, session_version, daily_limit, created_at FROM users WHERE username = ? COLLATE NOCASE;",
                (username.strip(),),
            )
            return cursor.fetchone()
        except sqlite3.OperationalError:
            cursor = conn.execute(
                "SELECT id, username, password_hash, role, is_active, last_login, session_version, created_at FROM users WHERE username = ? COLLATE NOCASE;",
                (username.strip(),),
            )
            return cursor.fetchone()


def get_user_by_id(
    user_id: int,
    db_path: str | Path | None = None,
) -> sqlite3.Row | None:
    """Retrieve a user by id."""
    with get_connection(db_path) as conn:
        try:
            cursor = conn.execute(
                "SELECT id, username, password_hash, role, is_active, last_login, session_version, daily_limit, created_at FROM users WHERE id = ?;",
                (user_id,),
            )
            return cursor.fetchone()
        except sqlite3.OperationalError:
            cursor = conn.execute(
                "SELECT id, username, password_hash, role, is_active, last_login, session_version, created_at FROM users WHERE id = ?;",
                (user_id,),
            )
            return cursor.fetchone()


def update_user_login(user_id: int, db_path: str | Path | None = None) -> None:
    """Update last_login timestamp for a user."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?;",
            (user_id,),
        )


def set_user_active(user_id: int, is_active: int, db_path: str | Path | None = None) -> None:
    """Set active status (1 or 0) for a user."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?;",
            (is_active, user_id),
        )


def update_user_session_version(user_id: int, session_version: int, db_path: str | Path | None = None) -> None:
    """Update session_version for a user to invalidate existing sessions."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE users SET session_version = ? WHERE id = ?;",
            (session_version, user_id),
        )


def create_session(
    idea: str,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Create a new session with an initial idea and optional user owner."""
    with get_connection(db_path) as conn:
        if user_id is not None:
            user_row = conn.execute("SELECT id FROM users WHERE id = ?;", (user_id,)).fetchone()
            if not user_row:
                user_id = None

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


def list_users_with_stats(db_path: str | Path | None = None) -> list[sqlite3.Row]:
    """Retrieve all users with created, last login, status, daily limit, prompt count, and calls today."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("""
            SELECT 
                u.id,
                u.username,
                u.role,
                u.created_at,
                u.last_login,
                u.is_active,
                u.session_version,
                u.daily_limit,
                COUNT(DISTINCT p.id) AS prompt_count,
                (SELECT COUNT(*) FROM llm_usage WHERE user_id = u.id AND date(timestamp) = date('now')) AS calls_today
            FROM users u
            LEFT JOIN sessions s ON u.id = s.user_id
            LEFT JOIN prompts p ON s.id = p.session_id
            GROUP BY u.id
            ORDER BY u.id ASC;
        """)
        return cursor.fetchall()


def toggle_user_active(user_id: int, db_path: str | Path | None = None) -> int:
    """Toggle is_active (1 -> 0, 0 -> 1). If disabled, increment session_version to log out."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT is_active FROM users WHERE id = ?;", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("User not found")
        new_status = 0 if row["is_active"] == 1 else 1
        if new_status == 0:
            conn.execute(
                "UPDATE users SET is_active = ?, session_version = session_version + 1 WHERE id = ?;",
                (new_status, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET is_active = ? WHERE id = ?;",
                (new_status, user_id),
            )
        return new_status


def increment_user_session_version(user_id: int, db_path: str | Path | None = None) -> int:
    """Increment session_version for a user, invalidating existing sessions."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT session_version FROM users WHERE id = ?;", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("User not found")
        new_version = int(row["session_version"]) + 1
        conn.execute("UPDATE users SET session_version = ? WHERE id = ?;", (new_version, user_id))
        return new_version


def reset_user_password(user_id: int, new_password_hash: str, db_path: str | Path | None = None) -> None:
    """Update password_hash and increment session_version for a user."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?;",
            (new_password_hash, user_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("User not found")


def delete_user_cascade(user_id: int, db_path: str | Path | None = None) -> bool:
    """Delete a user and all their associated sessions, messages, prompts, and llm_usage."""
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM llm_usage WHERE user_id = ?;", (user_id,))
        conn.execute(
            "DELETE FROM prompts WHERE session_id IN (SELECT id FROM sessions WHERE user_id = ?);",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE user_id = ?);",
            (user_id,),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?;", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id = ?;", (user_id,))
        return cursor.rowcount > 0


def record_llm_usage(
    user_id: int,
    route: str,
    success: int = 1,
    db_path: str | Path | None = None,
) -> None:
    """Log an LLM route call with user, route, timestamp, and success flag."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_usage (user_id, route, success) VALUES (?, ?, ?);",
            (user_id, route, success),
        )


def get_user_usage_today(
    user_id: int,
    db_path: str | Path | None = None,
) -> int:
    """Return the number of LLM calls made by the user for the current UTC day."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) AS cnt FROM llm_usage WHERE user_id = ? AND date(timestamp) = date('now');",
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0


def get_total_usage_today(db_path: str | Path | None = None) -> int:
    """Return the total number of LLM calls across all users for the current UTC day."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) AS cnt FROM llm_usage WHERE date(timestamp) = date('now');"
        )
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0


def set_user_daily_limit(
    user_id: int,
    daily_limit: int | None,
    db_path: str | Path | None = None,
) -> None:
    """Update a user's custom daily limit (None indicates fallback to env default)."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE users SET daily_limit = ? WHERE id = ?;",
            (daily_limit, user_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("User not found")


def record_failed_attempt(key: str, db_path: str | Path | None = None) -> None:
    """Record a timestamped failed attempt for rate limiting."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO failed_attempts (key, created_at) VALUES (?, ?);",
            (key, time.time()),
        )


def get_failed_attempts_count(
    key: str,
    window_seconds: float = 900.0,
    db_path: str | Path | None = None,
) -> int:
    """Return the number of failed attempts for a key in the last window_seconds (default 15 mins)."""
    cutoff = time.time() - window_seconds
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) AS cnt FROM failed_attempts WHERE key = ? AND created_at >= ?;",
            (key, cutoff),
        )
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0


def clear_failed_attempts(key: str, db_path: str | Path | None = None) -> None:
    """Clear all recorded failed attempts for a key upon successful authentication."""
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM failed_attempts WHERE key = ?;", (key,))


def record_admin_audit(
    action: str,
    admin_username: str | None = None,
    target_username: str | None = None,
    details: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Record an administrative or security event in admin_audit. Never stores passwords."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO admin_audit (admin_username, action, target_username, details) VALUES (?, ?, ?, ?);",
            (admin_username, action, target_username, details),
        )


def get_recent_admin_audit(
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[sqlite3.Row]:
    """Retrieve the most recent admin audit log entries (default 50), newest first."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT id, admin_username, action, target_username, details, timestamp FROM admin_audit ORDER BY id DESC LIMIT ?;",
            (limit,),
        )
        return cursor.fetchall()


def clear_admin_audit(db_path: str | Path | None = None) -> None:
    """Delete all records from the admin_audit table."""
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM admin_audit;")


