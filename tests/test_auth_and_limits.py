import json
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
import pytest
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

from app import create_app
from db import (
    add_message,
    create_session,
    create_user,
    get_connection,
    get_recent_admin_audit,
    get_user_by_id,
    get_user_by_username,
    get_user_usage_today,
    init_db,
    record_admin_audit,
    record_llm_usage,
    save_final_prompt,
    set_user_active,
    set_user_daily_limit,
    sync_admin,
    update_user_session_version,
)


class CSRFTestClient(FlaskClient):
    """Test client that automatically attaches session CSRF token unless explicitly overridden."""

    def open(self, *args: Any, **kwargs: Any) -> Any:
        headers = kwargs.setdefault("headers", {})
        with self.session_transaction() as sess:
            if "csrf_token" not in sess:
                sess["csrf_token"] = "test-csrf-token"
            csrf = sess["csrf_token"]
        if isinstance(headers, dict):
            if "X-CSRF-Token" not in headers:
                headers["X-CSRF-Token"] = csrf
        elif isinstance(headers, list):
            if not any(k.lower() == "x-csrf-token" for k, v in headers):
                headers.append(("X-CSRF-Token", csrf))
        return super().open(*args, **kwargs)


def login_as_admin(
    client: FlaskClient,
    username: str = "super_admin",
    password: str = "admin_pass123",
    gate_password: str = "gate_pass_123",
) -> None:
    """Helper to authenticate as admin through gate and admin login."""
    client.post("/admin/gate", json={"gate_password": gate_password})
    client.post("/admin/login", json={"username": username, "password": password})


@pytest.fixture
def auth_app(tmp_path: Path):
    """Provide a test application with an isolated database and rate limiting enabled."""
    test_db = tmp_path / "test_auth.db"
    app = create_app(
        db_path=test_db,
        test_config={
            "TESTING": True,
            "RATELIMIT_ENABLED": True,
            "RATELIMIT_STORAGE_URI": "memory://",
        },
    )
    app.test_client_class = CSRFTestClient
    return app


@pytest.fixture
def unauthenticated_client(auth_app) -> FlaskClient:
    """Provide an unauthenticated test client."""
    return auth_app.test_client()


@pytest.fixture
def authenticated_client(auth_app) -> FlaskClient:
    """Provide an authenticated test client with a created user in the database."""
    db_path = auth_app.config["DB_PATH"]
    user_id = create_user("alice", generate_password_hash("password123"), db_path=db_path)
    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "alice"
        sess["csrf_token"] = "test-csrf-token"
    return client


def test_unauthenticated_page_redirects_to_login(unauthenticated_client: FlaskClient) -> None:
    """Verify unauthenticated GET / redirects to /login."""
    resp = unauthenticated_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_unauthenticated_api_returns_401(unauthenticated_client: FlaskClient) -> None:
    """Verify unauthenticated API requests return 401 with consistent error shape."""
    resp = unauthenticated_client.get("/api/sessions")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Unauthorized"}

    resp_post = unauthenticated_client.post("/api/sessions", json={"idea": "test"})
    assert resp_post.status_code == 401
    assert resp_post.get_json() == {"error": "Unauthorized"}


def test_register_page_get(unauthenticated_client: FlaskClient, authenticated_client: FlaskClient) -> None:
    """Verify GET /register renders form for guests and redirects logged-in users."""
    resp = unauthenticated_client.get("/register")
    assert resp.status_code == 200
    assert b"Create account" in resp.data

    resp_auth = authenticated_client.get("/register")
    assert resp_auth.status_code == 302
    assert resp_auth.headers["Location"] in {"/", "http://localhost/"}


def test_register_validation_errors(unauthenticated_client: FlaskClient) -> None:
    """Verify registration input validation rules."""
    # Empty username
    resp = unauthenticated_client.post("/register", json={"username": "  ", "password": "password123"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Username is required"}

    # Short username
    resp = unauthenticated_client.post("/register", json={"username": "ab", "password": "password123"})
    assert resp.status_code == 400
    assert "between 3 and 30 characters" in resp.get_json()["error"]

    # Invalid characters in username
    resp = unauthenticated_client.post("/register", json={"username": "user@name!", "password": "password123"})
    assert resp.status_code == 400
    assert "letters, numbers, and underscores" in resp.get_json()["error"]

    # Short password
    resp = unauthenticated_client.post("/register", json={"username": "valid_user", "password": "123"})
    assert resp.status_code == 400
    assert "at least 6 characters" in resp.get_json()["error"]

    # Mismatched confirm password
    resp = unauthenticated_client.post(
        "/register",
        json={"username": "valid_user", "password": "password123", "confirm_password": "different123"},
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Passwords do not match"}


def test_register_success_and_duplicate(unauthenticated_client: FlaskClient) -> None:
    """Verify successful registration and duplicate username rejection."""
    # Successful JSON registration
    resp = unauthenticated_client.post(
        "/register",
        json={"username": "bob_smith", "password": "password123"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["ok"] is True
    assert data["username"] == "bob_smith"

    with unauthenticated_client.session_transaction() as sess:
        assert sess["authenticated"] is True
        assert sess["username"] == "bob_smith"
        assert sess["user_id"] == data["user_id"]

    # Attempt duplicate username with new client
    new_client = unauthenticated_client.application.test_client()
    resp_dup = new_client.post(
        "/register",
        json={"username": "bob_smith", "password": "password456"},
    )
    assert resp_dup.status_code == 400
    assert resp_dup.get_json() == {"error": "Username is already taken"}


def test_login_page_get(unauthenticated_client: FlaskClient, authenticated_client: FlaskClient) -> None:
    """Verify GET /login displays login form for guests and redirects authenticated users."""
    resp = unauthenticated_client.get("/login")
    assert resp.status_code == 200
    assert b"Sign in" in resp.data

    resp_auth = authenticated_client.get("/login")
    assert resp_auth.status_code == 302
    assert resp_auth.headers["Location"] in {"/", "http://localhost/"}


def test_login_invalid_credentials(unauthenticated_client: FlaskClient) -> None:
    """Verify POST /login with incorrect credentials returns 401."""
    # Unknown user
    resp = unauthenticated_client.post(
        "/login",
        json={"username": "nonexistent", "password": "password123"},
    )
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Invalid username or password"}

    # Form submission with invalid password
    resp_form = unauthenticated_client.post(
        "/login",
        data={"username": "alice", "password": "wrongpassword"},
    )
    assert resp_form.status_code == 401
    assert b"Invalid username or password" in resp_form.data


def test_login_success(auth_app, unauthenticated_client: FlaskClient) -> None:
    """Verify POST /login with valid credentials logs in successfully."""
    db_path = auth_app.config["DB_PATH"]
    create_user("charlie", generate_password_hash("mypassword"), db_path=db_path)

    resp = unauthenticated_client.post(
        "/login",
        json={"username": "charlie", "password": "mypassword"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with unauthenticated_client.session_transaction() as sess:
        assert sess["authenticated"] is True
        assert sess["username"] == "charlie"


def test_logout(authenticated_client: FlaskClient) -> None:
    """Verify GET /logout clears session and redirects to /login."""
    resp = authenticated_client.get("/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    with authenticated_client.session_transaction() as sess:
        assert not sess.get("authenticated")
        assert not sess.get("user_id")


def test_user_session_isolation(auth_app) -> None:
    """Verify that User A cannot view, access, or delete User B's sessions."""
    db_path = auth_app.config["DB_PATH"]
    user_a_id = create_user("usera", generate_password_hash("password123"), db_path=db_path)
    user_b_id = create_user("userb", generate_password_hash("password123"), db_path=db_path)

    # User A creates a session
    client_a = auth_app.test_client()
    with client_a.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_a_id
        sess["username"] = "usera"

    mock_reply = json.dumps({
        "status": "ask",
        "type": "coding",
        "question": "What language?",
    })
    with patch("services.builder.call_llm", return_value=mock_reply):
        resp_a = client_a.post("/api/sessions", json={"idea": "User A private project"})
        assert resp_a.status_code == 201
        session_a_id = resp_a.get_json()["session_id"]

    # User B logs in
    client_b = auth_app.test_client()
    with client_b.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_b_id
        sess["username"] = "userb"

    # User B lists sessions - User A's session should NOT appear
    resp_list = client_b.get("/api/sessions")
    assert resp_list.status_code == 200
    sessions = resp_list.get_json()
    assert len(sessions) == 0

    # User B attempts to access User A's session directly
    resp_get = client_b.get(f"/api/sessions/{session_a_id}")
    assert resp_get.status_code == 404
    assert resp_get.get_json() == {"error": "Session not found"}

    # User B attempts to delete User A's session
    resp_del = client_b.delete(f"/api/sessions/{session_a_id}")
    assert resp_del.status_code == 404
    assert resp_del.get_json() == {"error": "Session not found"}

    # User A can still access their session
    resp_a_check = client_a.get(f"/api/sessions/{session_a_id}")
    assert resp_a_check.status_code == 200
    assert resp_a_check.get_json()["idea"] == "User A private project"


def test_rate_limiting_on_llm_routes(authenticated_client: FlaskClient) -> None:
    """Verify LLM routes enforce the 20 requests/minute rate limit."""
    mock_reply = json.dumps({
        "status": "ask",
        "type": "coding",
        "question": "Which framework?",
    })

    with patch("services.builder.call_llm", return_value=mock_reply):
        # 20 requests within limit should succeed
        for i in range(20):
            resp = authenticated_client.post("/api/sessions", json={"idea": f"Idea {i}"})
            assert resp.status_code == 201, f"Request {i+1} failed with status {resp.status_code}"

        # 21st request should be blocked by rate limiter
        resp_blocked = authenticated_client.post("/api/sessions", json={"idea": "Blocked idea"})
        assert resp_blocked.status_code == 429
        assert resp_blocked.get_json() == {"error": "Rate limit exceeded"}


def test_stale_session_handling(auth_app) -> None:
    """Verify stale session cookies with deleted/non-existent user IDs are cleared and rejected."""
    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = 999999  # Non-existent user
        sess["username"] = "ghost_user"

    # API request should return 401 and clear session
    resp_api = client.get("/api/sessions")
    assert resp_api.status_code == 401
    assert resp_api.get_json() == {"error": "Unauthorized"}

    # Page request should redirect to /login
    resp_page = client.get("/")
    assert resp_page.status_code == 302
    assert "/login" in resp_page.headers["Location"]


def test_rate_limiting_per_user_buckets(auth_app) -> None:
    """Verify two different users each get their own 20/min bucket; user A 21st blocked while user B unaffected."""
    db_path = auth_app.config["DB_PATH"]
    user_a_id = create_user("rate_user_a", generate_password_hash("pass123"), db_path=db_path)
    user_b_id = create_user("rate_user_b", generate_password_hash("pass123"), db_path=db_path)

    client_a = auth_app.test_client()
    with client_a.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_a_id
        sess["username"] = "rate_user_a"

    client_b = auth_app.test_client()
    with client_b.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_b_id
        sess["username"] = "rate_user_b"

    mock_reply = json.dumps({
        "status": "ask",
        "type": "study",
        "question": "Which topic?",
    })

    with patch("services.builder.call_llm", return_value=mock_reply):
        # User A exhausts their 20 requests/minute bucket
        for i in range(20):
            resp = client_a.post("/api/sessions", json={"idea": f"User A Idea {i}"})
            assert resp.status_code == 201, f"User A request {i+1} failed with status {resp.status_code}"

        # User A's 21st request is blocked
        resp_a_blocked = client_a.post("/api/sessions", json={"idea": "User A Blocked"})
        assert resp_a_blocked.status_code == 429
        assert resp_a_blocked.get_json() == {"error": "Rate limit exceeded"}

        # User B is completely unaffected and can still make requests in their own bucket
        resp_b = client_b.post("/api/sessions", json={"idea": "User B Idea 1"})
        assert resp_b.status_code == 201
        assert resp_b.get_json()["status"] == "ask"


def test_disabled_user_is_blocked(auth_app) -> None:
    """Verify a disabled user cannot log in and an active user set to disabled has requests rejected."""
    db_path = auth_app.config["DB_PATH"]
    user_id = create_user("disabled_user", generate_password_hash("pass123"), db_path=db_path)
    set_user_active(user_id, 0, db_path=db_path)

    client = auth_app.test_client()

    # Attempt to log in with disabled account
    resp_login = client.post("/login", json={"username": "disabled_user", "password": "pass123"})
    assert resp_login.status_code == 401
    assert resp_login.get_json() == {"error": "account disabled"}

    # Now create an active user, log in, then disable the user mid-session
    active_id = create_user("active_to_disabled", generate_password_hash("pass123"), db_path=db_path)
    client_logged = auth_app.test_client()
    resp_ok = client_logged.post("/login", json={"username": "active_to_disabled", "password": "pass123"})
    assert resp_ok.status_code == 200

    # Disable the user in the database
    set_user_active(active_id, 0, db_path=db_path)

    # API request should be rejected with 401 and "account disabled"
    resp_api = client_logged.get("/api/sessions")
    assert resp_api.status_code == 401
    assert resp_api.get_json() == {"error": "account disabled"}

    # Session cookie should be cleared
    with client_logged.session_transaction() as sess:
        assert not sess.get("authenticated")
        assert not sess.get("user_id")

    # Page request should also be rejected with 401 and show "account disabled"
    with client_logged.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = active_id
        sess["username"] = "active_to_disabled"
        sess["session_version"] = 0

    resp_page = client_logged.get("/")
    assert resp_page.status_code == 401
    assert b"account disabled" in resp_page.data


def test_session_version_mismatch_logs_user_out(auth_app) -> None:
    """Verify a session version mismatch clears the session and rejects requests with 'logged out by admin'."""
    db_path = auth_app.config["DB_PATH"]
    user_id = create_user("versioned_user", generate_password_hash("pass123"), db_path=db_path)

    client = auth_app.test_client()
    resp_login = client.post("/login", json={"username": "versioned_user", "password": "pass123"})
    assert resp_login.status_code == 200

    # User's session version in DB is bumped (e.g. admin revoked session)
    update_user_session_version(user_id, 1, db_path=db_path)

    # Subsequent API request fails with 401 and "logged out by admin"
    resp_api = client.get("/api/sessions")
    assert resp_api.status_code == 401
    assert resp_api.get_json() == {"error": "logged out by admin"}

    # Session should be cleared
    with client.session_transaction() as sess:
        assert not sess.get("authenticated")
        assert not sess.get("user_id")

    # Subsequent page request with mismatched version also rejected
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "versioned_user"
        sess["session_version"] = 0  # old version

    resp_page = client.get("/")
    assert resp_page.status_code == 401
    assert b"logged out by admin" in resp_page.data


def test_non_admin_gets_404(auth_app, monkeypatch) -> None:
    """Verify non-admin users receive 404 on admin-required routes while admins are allowed."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "boss_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    create_user("regular_joe", generate_password_hash("pass123"), db_path=db_path)

    client_normal = auth_app.test_client()
    client_normal.post("/login", json={"username": "regular_joe", "password": "pass123"})

    client_admin = auth_app.test_client()
    login_as_admin(client_admin, "boss_admin", "pass123", "gate_pass_123")

    # Regular user gets 404
    resp_normal = client_normal.get("/api/admin/check")
    assert resp_normal.status_code == 404
    assert resp_normal.get_json() == {"error": "Not Found"}

    # Admin user gets 200
    resp_admin = client_admin.get("/api/admin/check")
    assert resp_admin.status_code == 200
    assert resp_admin.get_json() == {"ok": True, "admin": True}


def test_migration_keeps_existing_rows(tmp_path: Path) -> None:
    """Verify that startup migration adds missing columns without deleting or rewriting existing rows."""
    legacy_db_path = tmp_path / "legacy_migration.db"

    # Set up database with pre-migration schema (only 4 columns)
    conn = sqlite3.connect(str(legacy_db_path))
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?);",
        ("existing_user_1", "hash_one_123"),
    )
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?);",
        ("existing_user_2", "hash_two_456"),
    )
    conn.commit()
    conn.close()

    # Run init_db which executes the migration
    init_db(legacy_db_path)

    # Verify new columns exist
    conn = sqlite3.connect(str(legacy_db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("PRAGMA table_info(users);")
    columns = {row["name"]: row for row in cursor.fetchall()}

    assert "role" in columns
    assert "is_active" in columns
    assert "last_login" in columns
    assert "session_version" in columns

    # Verify existing rows are intact and have expected defaults
    user1 = get_user_by_username("existing_user_1", db_path=legacy_db_path)
    assert user1 is not None
    assert user1["id"] == 1
    assert user1["username"] == "existing_user_1"
    assert user1["password_hash"] == "hash_one_123"
    assert user1["role"] == "user"
    assert user1["is_active"] == 1
    assert user1["session_version"] == 0
    assert user1["last_login"] is None

    user2 = get_user_by_username("existing_user_2", db_path=legacy_db_path)
    assert user2 is not None
    assert user2["id"] == 2
    assert user2["username"] == "existing_user_2"
    assert user2["password_hash"] == "hash_two_456"
    assert user2["role"] == "user"
    assert user2["is_active"] == 1
    assert user2["session_version"] == 0
    assert user2["last_login"] is None

    # Verify total count is still 2
    count = conn.execute("SELECT COUNT(*) as cnt FROM users;").fetchone()["cnt"]
    assert count == 2
    conn.close()


def test_registration_ignores_submitted_role(auth_app) -> None:
    """Verify registration ignores any submitted role field and forces role='user'."""
    client = auth_app.test_client()
    resp = client.post(
        "/register",
        json={"username": "role_tester", "password": "password123", "role": "admin"},
    )
    assert resp.status_code == 201

    db_path = auth_app.config["DB_PATH"]
    user = get_user_by_username("role_tester", db_path=db_path)
    assert user is not None
    assert user["role"] == "user"


def test_registering_admin_username_rejected(auth_app, monkeypatch) -> None:
    """Verify registering the ADMIN_USERNAME (case-insensitively) is rejected with 'username unavailable'."""
    monkeypatch.setenv("ADMIN_USERNAME", "MasterAdmin")
    client = auth_app.test_client()

    # Lowercase
    resp1 = client.post(
        "/register",
        json={"username": "masteradmin", "password": "password123"},
    )
    assert resp1.status_code == 400
    assert resp1.get_json() == {"error": "username unavailable"}

    # Uppercase
    resp2 = client.post(
        "/register",
        json={"username": "MASTERADMIN", "password": "password123"},
    )
    assert resp2.status_code == 400
    assert resp2.get_json() == {"error": "username unavailable"}

    # Mixed case
    resp3 = client.post(
        "/register",
        json={"username": "MasterAdmin", "password": "password123"},
    )
    assert resp3.status_code == 400
    assert resp3.get_json() == {"error": "username unavailable"}


def test_database_rejects_second_admin(auth_app, monkeypatch) -> None:
    """Verify database unique partial index rejects having a second admin."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "first_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
    sync_admin(db_path)

    # Attempt to insert a second admin directly into the database
    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin');",
            ("second_admin", "hash123"),
        )
    conn.close()


def test_startup_sync_leaves_exactly_one_admin(tmp_path: Path, monkeypatch) -> None:
    """Verify startup sync demotes any other admin, promotes matching user, and leaves exactly one admin."""
    db_path = tmp_path / "sync_admin.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            last_login TEXT,
            session_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('old_admin1', 'hash1', 'admin');")
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('old_admin2', 'hash2', 'admin');")
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('target_admin', 'old_hash', 'user');")
    conn.commit()
    conn.close()

    monkeypatch.setenv("ADMIN_USERNAME", "target_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "new_secret_password")

    sync_admin(db_path)

    # Verify target_admin is promoted to admin and its password hash matches new password
    target = get_user_by_username("target_admin", db_path=db_path)
    assert target is not None
    assert target["role"] == "admin"
    from werkzeug.security import check_password_hash
    assert check_password_hash(target["password_hash"], "new_secret_password")

    # Verify other admins were demoted to user
    admin1 = get_user_by_username("old_admin1", db_path=db_path)
    assert admin1["role"] == "user"

    admin2 = get_user_by_username("old_admin2", db_path=db_path)
    assert admin2["role"] == "user"

    # Verify exactly one admin exists in the table
    conn = sqlite3.connect(str(db_path))
    admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin';").fetchone()[0]
    assert admin_count == 1
    conn.close()


def test_app_refuses_to_start_without_admin_env_vars(tmp_path: Path, monkeypatch) -> None:
    """Verify application refuses to start in production if ADMIN_USERNAME, ADMIN_PASSWORD, or ADMIN_GATE_PASSWORD is missing."""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_GATE_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="ADMIN_USERNAME, ADMIN_PASSWORD, and ADMIN_GATE_PASSWORD"):
        create_app(db_path=tmp_path / "prod_test.db")

    # Missing only gate password
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.delenv("ADMIN_GATE_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_GATE_PASSWORD"):
        create_app(db_path=tmp_path / "prod_test2.db")


def test_admin_page_renders_for_admin_and_404_for_non_admin(auth_app, monkeypatch) -> None:
    """Verify /admin renders for admin, returns 404 for non-admin, and returns 404 for unauthenticated guests."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    create_user("plain_user", generate_password_hash("pass123"), db_path=db_path)

    # Guest gets 404
    guest_client = auth_app.test_client()
    resp_guest = guest_client.get("/admin")
    assert resp_guest.status_code == 404
    assert resp_guest.get_json() == {"error": "Not Found"}

    # Non-admin gets 404
    user_client = auth_app.test_client()
    user_client.post("/login", json={"username": "plain_user", "password": "pass123"})
    resp_user = user_client.get("/admin")
    assert resp_user.status_code == 404
    assert resp_user.get_json() == {"error": "Not Found"}

    # Admin through gate and admin login gets 200
    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")
    resp_admin = admin_client.get("/admin")
    assert resp_admin.status_code == 200
    assert b"Admin Dashboard" in resp_admin.data
    assert b"plain_user" in resp_admin.data


def test_admin_toggle_status_action(auth_app, monkeypatch) -> None:
    """Verify admin can disable and enable another user."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    target_id = create_user("toggle_target", generate_password_hash("pass123"), db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    # Disable user
    resp_dis = admin_client.post(f"/admin/users/{target_id}/toggle-status")
    assert resp_dis.status_code == 200
    assert resp_dis.get_json() == {"ok": True, "is_active": 0}

    target = get_user_by_id(target_id, db_path=db_path)
    assert target["is_active"] == 0

    # Re-enable user
    resp_en = admin_client.post(f"/admin/users/{target_id}/toggle-status")
    assert resp_en.status_code == 200
    assert resp_en.get_json() == {"ok": True, "is_active": 1}

    target = get_user_by_id(target_id, db_path=db_path)
    assert target["is_active"] == 1


def test_admin_force_logout_action(auth_app, monkeypatch) -> None:
    """Verify admin can force logout another user by incrementing session_version."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    target_id = create_user("logout_target", generate_password_hash("pass123"), db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    resp = admin_client.post(f"/admin/users/{target_id}/force-logout")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "session_version": 1}

    target = get_user_by_id(target_id, db_path=db_path)
    assert target["session_version"] == 1


def test_admin_reset_password_action(auth_app, monkeypatch) -> None:
    """Verify admin can reset user password, receiving temporary password and bumping session_version."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    target_id = create_user("reset_target", generate_password_hash("old_pass123"), db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    resp = admin_client.post(f"/admin/users/{target_id}/reset-password")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    temp_password = data["temporary_password"]
    assert len(temp_password) >= 8

    # Target user logs in with new temporary password
    target_client = auth_app.test_client()
    resp_login = target_client.post("/login", json={"username": "reset_target", "password": temp_password})
    assert resp_login.status_code == 200

    # Old password fails
    bad_client = auth_app.test_client()
    resp_bad = bad_client.post("/login", json={"username": "reset_target", "password": "old_pass123"})
    assert resp_bad.status_code == 401


def test_admin_delete_user_cascade_action(auth_app, monkeypatch) -> None:
    """Verify admin can delete a user and all their sessions, messages, and prompts."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    target_id = create_user("delete_target", generate_password_hash("pass123"), db_path=db_path)
    session_id = create_session("Target idea", user_id=target_id, db_path=db_path)
    add_message(session_id, "user", "Message content", db_path=db_path)
    save_final_prompt(session_id, "Prompt content", db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    resp = admin_client.post(f"/admin/users/{target_id}/delete")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    # Verify user deleted
    assert get_user_by_id(target_id, db_path=db_path) is None

    # Verify cascading deletion
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?;", (target_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?;", (session_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM prompts WHERE session_id = ?;", (session_id,)).fetchone()[0] == 0
    conn.close()


def test_admin_self_protection_rules(auth_app, monkeypatch) -> None:
    """Verify an admin cannot disable, delete, or force-logout themselves."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "self_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    admin = get_user_by_username("self_admin", db_path=db_path)
    admin_id = admin["id"]

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "self_admin", "admin_pass123", "gate_pass_123")

    # Cannot disable self
    resp_dis = admin_client.post(f"/admin/users/{admin_id}/toggle-status")
    assert resp_dis.status_code == 400
    assert resp_dis.get_json() == {"error": "Admins cannot disable their own account"}

    # Cannot force logout self
    resp_out = admin_client.post(f"/admin/users/{admin_id}/force-logout")
    assert resp_out.status_code == 400
    assert resp_out.get_json() == {"error": "Admins cannot force logout their own account"}

    # Cannot delete self
    resp_del = admin_client.post(f"/admin/users/{admin_id}/delete")
    assert resp_del.status_code == 400
    assert resp_del.get_json() == {"error": "Admins cannot delete their own account"}


def test_admin_actions_return_404_for_non_admins(auth_app, monkeypatch) -> None:
    """Verify non-admin users receive 404 on every /admin route."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "main_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    sync_admin(db_path)

    target_id = create_user("victim_user", generate_password_hash("pass123"), db_path=db_path)
    create_user("attacker_user", generate_password_hash("pass123"), db_path=db_path)

    attacker_client = auth_app.test_client()
    attacker_client.post("/login", json={"username": "attacker_user", "password": "pass123"})

    # Every /admin POST route returns 404
    for route in [
        f"/admin/users/{target_id}/toggle-status",
        f"/admin/users/{target_id}/force-logout",
        f"/admin/users/{target_id}/reset-password",
        f"/admin/users/{target_id}/delete",
    ]:
        resp = attacker_client.post(route)
        assert resp.status_code == 404, f"Route {route} expected 404, got {resp.status_code}"
        assert resp.get_json() == {"error": "Not Found"}


def test_no_route_can_create_user_or_change_role(auth_app) -> None:
    """Verify there is no route to create an admin user or alter user roles."""
    # Ensure registration does not allow role elevation
    client = auth_app.test_client()
    resp = client.post("/register", json={"username": "fake_admin", "password": "password123", "role": "admin"})
    assert resp.status_code == 201
    user = get_user_by_username("fake_admin", db_path=auth_app.config["DB_PATH"])
    assert user["role"] == "user"

    # Inspect all registered routes on the app to verify no role-editing or admin-creation routes exist
    rules = [str(rule) for rule in auth_app.url_map.iter_rules()]
    for rule in rules:
        assert "role" not in rule.lower(), f"Suspicious role-editing route found: {rule}"
        assert "create-user" not in rule.lower(), f"Suspicious user-creation route found: {rule}"
        assert "add-user" not in rule.lower(), f"Suspicious user-addition route found: {rule}"


def test_admin_ui_link_visibility(auth_app, monkeypatch) -> None:
    """Verify the /admin link in the main UI is visible to admins and hidden from regular users."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "admin_guy")
    monkeypatch.setenv("ADMIN_PASSWORD", "pass123")
    sync_admin(db_path)

    create_user("regular_gal", generate_password_hash("pass123"), db_path=db_path)

    # Regular user does not see /admin link
    client_reg = auth_app.test_client()
    client_reg.post("/login", json={"username": "regular_gal", "password": "pass123"})
    resp_reg = client_reg.get("/")
    assert resp_reg.status_code == 200
    assert b'href="/admin"' not in resp_reg.data

    # Admin sees /admin link
    client_admin = auth_app.test_client()
    client_admin.post("/login", json={"username": "admin_guy", "password": "pass123"})
    resp_admin = client_admin.get("/")
    assert resp_admin.status_code == 200
    assert b'href="/admin"' in resp_admin.data


def test_no_gate_means_404(auth_app, monkeypatch) -> None:
    """Verify accessing /admin/login or /admin without going through gate returns 404."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    client = auth_app.test_client()

    # GET /admin/login without gate -> 404
    resp = client.get("/admin/login")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # POST /admin/login without gate -> 404
    resp = client.post("/admin/login", json={"username": "super_admin", "password": "admin_pass123"})
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # GET /admin without gate -> 404
    resp = client.get("/admin")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}


def test_expired_gate_means_404(auth_app, monkeypatch) -> None:
    """Verify an expired gate_until session causes /admin/login to return 404."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    client = auth_app.test_client()

    # Successfully unlock gate
    resp = client.post("/admin/gate", json={"gate_password": "gate_pass_123"})
    assert resp.status_code == 200

    # Before expiry, GET /admin/login returns 200
    resp = client.get("/admin/login")
    assert resp.status_code == 200
    assert b"Admin Authentication" in resp.data

    # Expire the gate in session
    with client.session_transaction() as sess:
        sess["gate_until"] = time.time() - 10

    # Now GET /admin/login returns 404
    resp = client.get("/admin/login")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # POST /admin/login also returns 404
    resp = client.post("/admin/login", json={"username": "super_admin", "password": "admin_pass123"})
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}


def test_normal_login_cannot_reach_admin(auth_app, monkeypatch) -> None:
    """Verify logging in through the normal user login form never grants admin access."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    client = auth_app.test_client()

    # Normal login with admin credentials
    resp = client.post("/login", json={"username": "super_admin", "password": "admin_pass123"})
    assert resp.status_code == 200

    # Cannot reach /admin
    resp = client.get("/admin")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # Cannot reach /api/admin/check
    resp = client.get("/api/admin/check")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # Cannot invoke admin action
    resp = client.post("/admin/users/1/toggle-status")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}


def test_lockout_after_five_failures(auth_app, monkeypatch) -> None:
    """Verify rate limiting locks out after 5 failed attempts per 15 minutes."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    # 1. Test Gate lockout
    gate_client = auth_app.test_client()
    gate_client.environ_base["REMOTE_ADDR"] = "192.168.1.100"
    for i in range(5):
        resp = gate_client.post("/admin/gate", json={"gate_password": "wrong_gate_pass"})
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Invalid gate password"}

    # 6th attempt -> 429
    resp = gate_client.post("/admin/gate", json={"gate_password": "wrong_gate_pass"})
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.get_json()["error"]

    # Even correct password is now locked out
    resp = gate_client.post("/admin/gate", json={"gate_password": "gate_pass_123"})
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.get_json()["error"]

    # 2. Test Admin login lockout (use different IP to isolate)
    login_client = auth_app.test_client()
    login_client.environ_base["REMOTE_ADDR"] = "192.168.1.101"
    # Open gate
    resp = login_client.post("/admin/gate", json={"gate_password": "gate_pass_123"})
    assert resp.status_code == 200

    for i in range(5):
        resp = login_client.post(
            "/admin/login",
            json={"username": "super_admin", "password": "wrong_admin_pass"},
        )
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Invalid credentials"}

    # 6th attempt -> 429
    resp = login_client.post(
        "/admin/login",
        json={"username": "super_admin", "password": "wrong_admin_pass"},
    )
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.get_json()["error"]

    # Even correct password is now locked out
    resp = login_client.post(
        "/admin/login",
        json={"username": "super_admin", "password": "admin_pass123"},
    )
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.get_json()["error"]


def test_csrf_rejection(auth_app) -> None:
    """Verify POST routes reject requests with missing or invalid CSRF tokens."""
    client = auth_app.test_client()

    # Invalid CSRF token
    with client.session_transaction() as sess:
        sess["csrf_token"] = "correct-csrf-token"

    resp = client.post(
        "/login",
        json={"username": "any_user", "password": "password123"},
        headers={"X-CSRF-Token": "invalid-csrf-token"},
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "CSRF token missing or invalid"}

    # Missing CSRF token in header and form
    resp = client.post(
        "/login",
        json={"username": "any_user", "password": "password123"},
        headers={"X-CSRF-Token": ""},
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "CSRF token missing or invalid"}


def test_admin_logout_clears_flags(auth_app, monkeypatch) -> None:
    """Verify POST /admin/logout clears admin_until and gate_until."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    client = auth_app.test_client()
    login_as_admin(client, "super_admin", "admin_pass123", "gate_pass_123")

    # Confirm admin access
    resp = client.get("/admin")
    assert resp.status_code == 200

    # Log out of admin
    resp = client.post("/admin/logout")
    assert resp.status_code in {200, 302}

    # /admin is now 404
    resp = client.get("/admin")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}

    # /admin/login is now 404 (gate_until was also cleared)
    resp = client.get("/admin/login")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Not Found"}


def test_daily_limit_enforced(auth_app, monkeypatch) -> None:
    """Verify daily limit is enforced and returns 429 when quota is reached for the day."""
    db_path = auth_app.config["DB_PATH"]
    user_id = create_user("limit_user", generate_password_hash("password123"), db_path=db_path)
    # Set custom daily limit = 2
    set_user_daily_limit(user_id, 2, db_path=db_path)

    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "limit_user"

    mock_reply = json.dumps({
        "status": "ask",
        "type": "coding",
        "question": "What language?",
    })

    with patch("services.builder.call_llm", return_value=mock_reply):
        # 1st call: OK
        resp1 = client.post("/api/sessions", json={"idea": "First call"})
        assert resp1.status_code == 201

        # 2nd call: OK
        resp2 = client.post("/api/sessions", json={"idea": "Second call"})
        assert resp2.status_code == 201

        # 3rd call: Exceeded limit -> 429
        resp3 = client.post("/api/sessions", json={"idea": "Third call"})
        assert resp3.status_code == 429
        assert resp3.get_json() == {"error": "Daily limit reached"}

        # Verify other LLM routes are also blocked
        resp_improve = client.post("/api/improve", json={"prompt": "Some prompt to improve"})
        assert resp_improve.status_code == 429
        assert resp_improve.get_json() == {"error": "Daily limit reached"}


def test_daily_limit_resets_next_utc_day(auth_app) -> None:
    """Verify that usage from prior UTC days does not count toward today's limit."""
    db_path = auth_app.config["DB_PATH"]
    user_id = create_user("utc_user", generate_password_hash("password123"), db_path=db_path)
    set_user_daily_limit(user_id, 2, db_path=db_path)

    # Insert 2 calls with yesterday's UTC date
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_usage (user_id, route, timestamp, success) VALUES (?, ?, datetime('now', '-1 day'), 1);",
            (user_id, "/api/sessions"),
        )
        conn.execute(
            "INSERT INTO llm_usage (user_id, route, timestamp, success) VALUES (?, ?, datetime('now', '-1 day'), 1);",
            (user_id, "/api/sessions"),
        )

    # Confirm calls today is 0
    assert get_user_usage_today(user_id, db_path=db_path) == 0

    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "utc_user"

    mock_reply = json.dumps({
        "status": "ask",
        "type": "coding",
        "question": "What language?",
    })

    with patch("services.builder.call_llm", return_value=mock_reply):
        # Should succeed because yesterday's calls don't count
        resp = client.post("/api/sessions", json={"idea": "Call today"})
        assert resp.status_code == 201

        # Now add one more call for today, reaching 2 for today
        resp2 = client.post("/api/sessions", json={"idea": "Second call today"})
        assert resp2.status_code == 201

        # Third call today hits the limit
        resp3 = client.post("/api/sessions", json={"idea": "Third call today"})
        assert resp3.status_code == 429
        assert resp3.get_json() == {"error": "Daily limit reached"}


def test_admin_exempt_from_daily_limit(auth_app, monkeypatch) -> None:
    """Verify that admin accounts are exempt from daily LLM limits."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "admin_boss")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    monkeypatch.setenv("DAILY_LIMIT", "1")
    sync_admin(db_path)

    admin_user = get_user_by_username("admin_boss", db_path=db_path)
    assert admin_user is not None
    assert admin_user["role"] == "admin"

    # Insert 5 calls for admin today
    for _ in range(5):
        record_llm_usage(admin_user["id"], "/api/sessions", success=1, db_path=db_path)

    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
        sess["role"] = "admin"

    mock_reply = json.dumps({
        "status": "ask",
        "type": "coding",
        "question": "What language?",
    })

    with patch("services.builder.call_llm", return_value=mock_reply):
        # Admin should NOT receive 429 despite having 5 calls with DAILY_LIMIT=1
        resp = client.post("/api/sessions", json={"idea": "Admin prompt"})
        assert resp.status_code == 201


def test_admin_set_daily_limit(auth_app, monkeypatch) -> None:
    """Verify admin can update a user's daily limit via POST /admin/users/<id>/set-limit."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    target_id = create_user("target_user", generate_password_hash("password123"), db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    # 1. Set custom limit to 50
    resp = admin_client.post(f"/admin/users/{target_id}/set-limit", json={"daily_limit": 50})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["daily_limit"] == 50
    updated_user = get_user_by_id(target_id, db_path=db_path)
    assert updated_user["daily_limit"] == 50

    # 2. Reset limit to None (fallback to default) with null/empty
    resp_reset = admin_client.post(f"/admin/users/{target_id}/set-limit", json={"daily_limit": None})
    assert resp_reset.status_code == 200
    assert resp_reset.get_json()["daily_limit"] is None
    reset_user = get_user_by_id(target_id, db_path=db_path)
    assert reset_user["daily_limit"] is None

    # 3. Negative limit returns 400
    resp_neg = admin_client.post(f"/admin/users/{target_id}/set-limit", json={"daily_limit": -5})
    assert resp_neg.status_code == 400
    assert "0 or greater" in resp_neg.get_json()["error"]

    # 4. Non-existent user returns 404
    resp_404 = admin_client.post("/admin/users/99999/set-limit", json={"daily_limit": 10})
    assert resp_404.status_code == 404

    # 5. Non-admin gets 404
    normal_client = auth_app.test_client()
    with normal_client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = target_id
        sess["username"] = "target_user"
        sess["role"] = "user"
    resp_unauth = normal_client.post(f"/admin/users/{target_id}/set-limit", json={"daily_limit": 10})
    assert resp_unauth.status_code == 404


def test_delete_user_cascades_llm_usage(auth_app, monkeypatch) -> None:
    """Verify deleting a user cascades and removes their llm_usage rows."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    sync_admin(db_path)

    victim_id = create_user("victim_user", generate_password_hash("password123"), db_path=db_path)

    # Insert usage records
    record_llm_usage(victim_id, "/api/sessions", success=1, db_path=db_path)
    record_llm_usage(victim_id, "/api/improve", success=1, db_path=db_path)

    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT COUNT(*) AS cnt FROM llm_usage WHERE user_id = ?;", (victim_id,))
        assert cursor.fetchone()["cnt"] == 2

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_pass_123")

    # Admin deletes victim user
    resp = admin_client.post(f"/admin/users/{victim_id}/delete")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # Check user is deleted
    assert get_user_by_id(victim_id, db_path=db_path) is None

    # Verify llm_usage rows were deleted
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT COUNT(*) AS cnt FROM llm_usage WHERE user_id = ?;", (victim_id,))
        assert cursor.fetchone()["cnt"] == 0


def test_audit_gate_events(auth_app, monkeypatch) -> None:
    """Verify gate failures and successes create admin_audit entries."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_secret_123")
    client = auth_app.test_client()

    # 1. Gate failure
    resp_fail = client.post("/admin/gate", json={"gate_password": "wrong_password"})
    assert resp_fail.status_code == 401

    logs = get_recent_admin_audit(50, db_path=db_path)
    assert any(log["action"] == "gate_failure" and "wrong_password" not in (log["details"] or "") for log in logs)

    # 2. Gate success
    resp_ok = client.post("/admin/gate", json={"gate_password": "gate_secret_123"})
    assert resp_ok.status_code == 200

    logs = get_recent_admin_audit(50, db_path=db_path)
    assert any(log["action"] == "gate_success" and "gate_secret_123" not in (log["details"] or "") for log in logs)


def test_audit_admin_login_events(auth_app, monkeypatch) -> None:
    """Verify admin login failures and successes create admin_audit entries."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_secret_123")
    sync_admin(db_path)

    client = auth_app.test_client()
    # Open gate
    client.post("/admin/gate", json={"gate_password": "gate_secret_123"})

    # 1. Admin login failure
    resp_fail = client.post(
        "/admin/login",
        json={"username": "super_admin", "password": "wrong_password"},
    )
    assert resp_fail.status_code == 401

    logs = get_recent_admin_audit(50, db_path=db_path)
    fail_entry = next((l for l in logs if l["action"] == "admin_login_failure"), None)
    assert fail_entry is not None
    assert fail_entry["admin_username"] == "super_admin"
    assert "wrong_password" not in (fail_entry["details"] or "")

    # 2. Admin login success
    resp_ok = client.post(
        "/admin/login",
        json={"username": "super_admin", "password": "admin_pass123"},
    )
    assert resp_ok.status_code == 200

    logs = get_recent_admin_audit(50, db_path=db_path)
    success_entry = next((l for l in logs if l["action"] == "admin_login_success"), None)
    assert success_entry is not None
    assert success_entry["admin_username"] == "super_admin"
    assert "admin_pass123" not in (success_entry["details"] or "")


def test_audit_admin_actions(auth_app, monkeypatch) -> None:
    """Verify every admin action (disable, enable, force logout, password reset, delete, limit change) creates an audit entry."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_secret_123")
    sync_admin(db_path)

    target_id = create_user("bob_target", generate_password_hash("password123"), db_path=db_path)
    assert target_id is not None

    client = auth_app.test_client()
    login_as_admin(client, "super_admin", "admin_pass123", "gate_secret_123")

    # 1. Disable user
    resp_dis = client.post(f"/admin/users/{target_id}/toggle-status")
    assert resp_dis.status_code == 200
    assert resp_dis.get_json()["is_active"] == 0
    logs = get_recent_admin_audit(50, db_path=db_path)
    assert any(
        l["action"] == "disable" and l["admin_username"] == "super_admin" and l["target_username"] == "bob_target"
        for l in logs
    )

    # 2. Enable user
    resp_en = client.post(f"/admin/users/{target_id}/toggle-status")
    assert resp_en.status_code == 200
    assert resp_en.get_json()["is_active"] == 1
    logs = get_recent_admin_audit(50, db_path=db_path)
    assert any(
        l["action"] == "enable" and l["admin_username"] == "super_admin" and l["target_username"] == "bob_target"
        for l in logs
    )

    # 3. Force logout
    resp_lo = client.post(f"/admin/users/{target_id}/force-logout")
    assert resp_lo.status_code == 200
    logs = get_recent_admin_audit(50, db_path=db_path)
    assert any(
        l["action"] == "force_logout" and l["admin_username"] == "super_admin" and l["target_username"] == "bob_target"
        for l in logs
    )

    # 4. Password reset (ensure temporary password is NEVER stored in audit)
    resp_pw = client.post(f"/admin/users/{target_id}/reset-password")
    assert resp_pw.status_code == 200
    temp_pw = resp_pw.get_json()["temporary_password"]
    logs = get_recent_admin_audit(50, db_path=db_path)
    pw_entry = next((l for l in logs if l["action"] == "password_reset" and l["target_username"] == "bob_target"), None)
    assert pw_entry is not None
    assert pw_entry["admin_username"] == "super_admin"
    assert temp_pw not in (pw_entry["details"] or "")
    assert temp_pw not in (pw_entry["admin_username"] or "")
    assert temp_pw not in (pw_entry["target_username"] or "")

    # 5. Limit change
    resp_lim = client.post(f"/admin/users/{target_id}/set-limit", json={"daily_limit": 42})
    assert resp_lim.status_code == 200
    logs = get_recent_admin_audit(50, db_path=db_path)
    lim_entry = next((l for l in logs if l["action"] == "limit_change" and l["target_username"] == "bob_target"), None)
    assert lim_entry is not None
    assert lim_entry["admin_username"] == "super_admin"
    assert "42" in lim_entry["details"]

    # 6. Delete user (ensure entry survives and stores target_username)
    resp_del = client.post(f"/admin/users/{target_id}/delete")
    assert resp_del.status_code == 200
    assert get_user_by_id(target_id, db_path=db_path) is None
    logs = get_recent_admin_audit(50, db_path=db_path)
    del_entry = next((l for l in logs if l["action"] == "delete" and l["target_username"] == "bob_target"), None)
    assert del_entry is not None
    assert del_entry["admin_username"] == "super_admin"
    assert del_entry["target_username"] == "bob_target"


def test_audit_survives_user_deletion(auth_app, monkeypatch) -> None:
    """Verify that audit entries survive user deletion since usernames rather than IDs are stored."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_secret_123")
    sync_admin(db_path)

    target_id = create_user("survivor_user", generate_password_hash("password123"), db_path=db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_secret_123")

    # Perform action on survivor_user
    admin_client.post(f"/admin/users/{target_id}/toggle-status")
    admin_client.post(f"/admin/users/{target_id}/delete")

    # User is gone
    assert get_user_by_id(target_id, db_path=db_path) is None

    # Check that audit rows still have target_username = 'survivor_user'
    with get_connection(db_path) as conn:
        cursor = conn.execute("SELECT action, target_username FROM admin_audit WHERE target_username = 'survivor_user';")
        rows = cursor.fetchall()
        assert len(rows) >= 2
        actions = [r["action"] for r in rows]
        assert "disable" in actions
        assert "delete" in actions


def test_audit_table_rendered_on_admin_page(auth_app, monkeypatch) -> None:
    """Verify /admin renders the Admin Audit Log card and table with entries."""
    db_path = auth_app.config["DB_PATH"]
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_secret_123")
    sync_admin(db_path)

    admin_client = auth_app.test_client()
    login_as_admin(admin_client, "super_admin", "admin_pass123", "gate_secret_123")

    resp = admin_client.get("/admin")
    assert resp.status_code == 200
    assert b"Admin Audit Log" in resp.data
    assert b"gate_success" in resp.data
    assert b"admin_login_success" in resp.data


def test_login_page_no_visible_admin_links(unauthenticated_client: FlaskClient) -> None:
    """Verify that GET /login has no visible admin link, button, corner element, or hint text."""
    resp = unauthenticated_client.get("/login")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert 'href="/admin/gate"' not in html
    assert "admin-gate-link" not in html
    assert "admin-gate-corner" not in html
    assert ">Admin</a>" not in html
    assert ">Admin</button>" not in html


def test_hidden_admin_entry_client_script() -> None:
    """Verify hidden admin entry client logic:
    1. Desktop shortcut: 'g' then 'a' in sequence within 1s navigates to /admin/gate.
    2. Typing in login form (username/password) never triggers navigation.
    3. Mobile long-press navigates after 5 seconds.
    4. Press under 5 seconds does nothing.
    """
    login_html_path = Path(__file__).resolve().parent.parent / "templates" / "login.html"
    assert login_html_path.exists(), "login.html must exist"
    content = login_html_path.read_text(encoding="utf-8")

    # Extract script tag content
    script_start = content.find("<script>")
    script_end = content.rfind("</script>")
    assert script_start != -1 and script_end != -1, "login.html must contain <script> tag"
    script_code = content[script_start + len("<script>"):script_end].strip()

    encoded_code = json.dumps(script_code)

    node_runner = f"""
    const assert = require('assert');

    function createMockEnvironment() {{
        const docListeners = {{}};
        const logoListeners = {{}};
        const logoClasses = new Set();
        let activeElement = null;
        const windowLocation = {{ href: "" }};
        let vibrateCount = 0;

        const mockLogo = {{
            id: "app-logo",
            classList: {{
                add: (c) => logoClasses.add(c),
                remove: (c) => logoClasses.delete(c),
                contains: (c) => logoClasses.has(c)
            }},
            addEventListener: (ev, fn) => {{
                logoListeners[ev] = logoListeners[ev] || [];
                logoListeners[ev].push(fn);
            }},
            dispatchEvent: (ev, evtObj) => {{
                if (logoListeners[ev]) {{
                    logoListeners[ev].forEach(fn => fn(evtObj || {{}}));
                }}
            }}
        }};

        const mockDoc = {{
            get activeElement() {{
                return activeElement;
            }},
            set activeElement(el) {{
                activeElement = el;
            }},
            getElementById: (id) => (id === "app-logo" ? mockLogo : null),
            querySelector: (sel) => (sel === ".login-header .logo" ? mockLogo : null),
            addEventListener: (ev, fn) => {{
                docListeners[ev] = docListeners[ev] || [];
                docListeners[ev].push(fn);
            }},
            dispatchEvent: (ev, evtObj) => {{
                if (docListeners[ev]) {{
                    docListeners[ev].forEach(fn => fn(evtObj || {{}}));
                }}
            }}
        }};

        const mockNav = {{
            vibrate: (ms) => {{ vibrateCount++; }}
        }};

        // Execute the extracted script
        const scriptSource = {encoded_code};
        const runScript = new Function("document", "window", "navigator", scriptSource);
        runScript(mockDoc, {{ location: windowLocation }}, mockNav);

        return {{
            mockDoc,
            mockLogo,
            logoClasses,
            windowLocation,
            setActiveElement: (el) => {{ activeElement = el; }},
            dispatchKey: (key, target, opts) => {{
                mockDoc.dispatchEvent("keydown", {{
                    key,
                    target: target || activeElement || null,
                    ctrlKey: (opts && opts.ctrlKey) || false,
                    altKey: (opts && opts.altKey) || false,
                    metaKey: (opts && opts.metaKey) || false,
                    preventDefault: () => {{}}
                }});
            }},
            dispatchLogoPointerDown: (btn) => {{
                mockLogo.dispatchEvent("pointerdown", {{
                    button: btn !== undefined ? btn : 0,
                    preventDefault: () => {{}}
                }});
            }},
            dispatchLogoPointerUp: () => {{
                mockLogo.dispatchEvent("pointerup", {{ preventDefault: () => {{}} }});
            }}
        }};
    }}

    // 1. Desktop shortcut: 'g' then 'a' within 1s when no input focused -> navigates
    (function testDesktopShortcut() {{
        const env = createMockEnvironment();
        env.setActiveElement(null);
        env.dispatchKey("g");
        env.dispatchKey("a");
        assert.strictEqual(env.windowLocation.href, "/admin/gate", "Desktop shortcut 'g' then 'a' must navigate to /admin/gate");
    }})();

    // 2. Normal typing in login form (username/password focused) -> never navigates
    (function testTypingInInputDoesNotTrigger() {{
        const env = createMockEnvironment();
        const inputElem = {{ tagName: "INPUT", isContentEditable: false }};
        env.setActiveElement(inputElem);
        env.dispatchKey("g", inputElem);
        env.dispatchKey("a", inputElem);
        assert.strictEqual(env.windowLocation.href, "", "Typing 'g' and 'a' while input is focused must NOT navigate");

        const textareaElem = {{ tagName: "TEXTAREA", isContentEditable: false }};
        env.setActiveElement(textareaElem);
        env.dispatchKey("g", textareaElem);
        env.dispatchKey("a", textareaElem);
        assert.strictEqual(env.windowLocation.href, "", "Typing in textarea must NOT navigate");
    }})();

    // 3. Desktop sequence with >1s gap -> does not navigate
    (function testDesktopSlowSequence() {{
        const realDateNow = Date.now;
        let simTime = 1000;
        Date.now = () => simTime;
        try {{
            const env = createMockEnvironment();
            env.setActiveElement(null);
            env.dispatchKey("g");
            simTime += 1200; // 1.2s later
            env.dispatchKey("a");
            assert.strictEqual(env.windowLocation.href, "", "Pressing 'g' then 'a' after >1s must NOT navigate");
        }} finally {{
            Date.now = realDateNow;
        }}
    }})();

    // 4. Mobile long-press navigates after 5 seconds
    (function testMobileLongPress5Seconds() {{
        let timerCallback = null;
        let timerMs = 0;
        const origSetTimeout = setTimeout;
        const origClearTimeout = clearTimeout;
        try {{
            global.setTimeout = (fn, ms) => {{ timerCallback = fn; timerMs = ms; return 123; }};
            global.clearTimeout = (id) => {{ timerCallback = null; }};

            const env = createMockEnvironment();
            env.dispatchLogoPointerDown(0);
            assert.strictEqual(timerMs, 5000, "Long press timer must be 5000ms");
            assert.strictEqual(env.logoClasses.has("press-pulse"), true, "Must show subtle visual pulse on start");
            assert.strictEqual(env.windowLocation.href, "", "Must not navigate immediately");

            // Advance 5 seconds
            assert.ok(timerCallback, "Timer callback must be registered");
            timerCallback();
            assert.strictEqual(env.windowLocation.href, "/admin/gate", "Must navigate to /admin/gate after 5s");
            assert.strictEqual(env.logoClasses.has("press-pulse"), false, "Must remove pulse class after navigation");
        }} finally {{
            global.setTimeout = origSetTimeout;
            global.clearTimeout = origClearTimeout;
        }}
    }})();

    // 5. Mobile press under 5 seconds cancels and does nothing
    (function testMobilePressUnder5Seconds() {{
        let timerCallback = null;
        const origSetTimeout = setTimeout;
        const origClearTimeout = clearTimeout;
        try {{
            global.setTimeout = (fn, ms) => {{ timerCallback = fn; return 456; }};
            global.clearTimeout = (id) => {{ timerCallback = null; }};

            const env = createMockEnvironment();
            env.dispatchLogoPointerDown(0);
            assert.strictEqual(env.logoClasses.has("press-pulse"), true);
            assert.ok(timerCallback !== null);

            // Released under 5 seconds (e.g. pointerup)
            env.dispatchLogoPointerUp();
            assert.strictEqual(timerCallback, null, "Timer must be cancelled when released early");
            assert.strictEqual(env.logoClasses.has("press-pulse"), false, "Pulse class must be removed when released");
            assert.strictEqual(env.windowLocation.href, "", "Must NOT navigate if released under 5 seconds");
        }} finally {{
            global.setTimeout = origSetTimeout;
            global.clearTimeout = origClearTimeout;
        }}
    }})();

    console.log("HIDDEN_ADMIN_ENTRY_TESTS_PASSED");
    """

    res = subprocess.run(["node", "-e", node_runner], capture_output=True, text=True)
    assert res.returncode == 0, f"Node tests failed: {res.stderr}\n{res.stdout}"
    assert "HIDDEN_ADMIN_ENTRY_TESTS_PASSED" in res.stdout








