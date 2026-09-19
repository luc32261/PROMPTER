import json
from pathlib import Path
from unittest.mock import patch
import pytest
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

from app import create_app
from db import create_user


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
