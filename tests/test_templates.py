import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
import pytest
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

from app import create_app
from db import (
    create_template,
    create_user,
    get_connection,
    get_recent_admin_audit,
    get_template_by_id,
    init_db,
    list_templates,
    sync_admin,
)


class CSRFTestClient(FlaskClient):
    """Test client that automatically attaches session CSRF token."""

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
def tpl_app(tmp_path: Path, monkeypatch):
    """Provide a test application with an isolated database and test admin credentials."""
    test_db = tmp_path / "test_templates.db"
    monkeypatch.setenv("ADMIN_USERNAME", "super_admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin_pass123")
    monkeypatch.setenv("ADMIN_GATE_PASSWORD", "gate_pass_123")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-mock")

    app = create_app(
        db_path=test_db,
        test_config={
            "TESTING": True,
            "RATELIMIT_ENABLED": True,
            "RATELIMIT_STORAGE_URI": "memory://",
        },
    )
    app.test_client_class = CSRFTestClient
    sync_admin(test_db)
    return app


@pytest.fixture
def guest_client(tpl_app) -> FlaskClient:
    """Provide an unauthenticated guest test client."""
    return tpl_app.test_client()


@pytest.fixture
def auth_client(tpl_app) -> FlaskClient:
    """Provide an authenticated regular user test client."""
    db_path = tpl_app.config["DB_PATH"]
    user_id = create_user("bob", generate_password_hash("password123"), db_path=db_path)
    client = tpl_app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "bob"
        sess["csrf_token"] = "test-csrf-token"
    return client


@pytest.fixture
def admin_client(tpl_app) -> FlaskClient:
    """Provide an authenticated admin client who has cleared the gate."""
    client = tpl_app.test_client()
    login_as_admin(client, "super_admin", "admin_pass123", "gate_pass_123")
    return client


def test_public_get_templates_empty_table(guest_client: FlaskClient, auth_client: FlaskClient) -> None:
    """Verify GET /api/templates returns empty list when table is empty and 404 for nonexistent template."""
    # Guest user can browse empty templates
    resp_guest = guest_client.get("/api/templates")
    assert resp_guest.status_code == 200
    assert resp_guest.get_json() == []

    # Authenticated user can browse empty templates
    resp_auth = auth_client.get("/api/templates")
    assert resp_auth.status_code == 200
    assert resp_auth.get_json() == []

    # Nonexistent template returns 404
    resp_not_found = guest_client.get("/api/templates/999")
    assert resp_not_found.status_code == 404
    assert resp_not_found.get_json() == {"error": "Template not found"}


def test_template_crud_admin_and_validation(admin_client: FlaskClient, tpl_app) -> None:
    """Verify admin can create, update, and delete templates, with full input validation and audit logging."""
    db_path = tpl_app.config["DB_PATH"]

    # 1. Validation on create
    # Missing title
    resp_err1 = admin_client.post(
        "/api/templates",
        json={"category": "study", "content": "Sample content"},
    )
    assert resp_err1.status_code == 400
    assert resp_err1.get_json() == {"error": "Title is required"}

    # Blank title
    resp_err2 = admin_client.post(
        "/api/templates",
        json={"title": "   ", "category": "study", "content": "Sample content"},
    )
    assert resp_err2.status_code == 400
    assert resp_err2.get_json() == {"error": "Title is required"}

    # Missing content
    resp_err3 = admin_client.post(
        "/api/templates",
        json={"title": "Study Guide", "category": "study"},
    )
    assert resp_err3.status_code == 400
    assert resp_err3.get_json() == {"error": "Content is required"}

    # Invalid category
    resp_err4 = admin_client.post(
        "/api/templates",
        json={"title": "Study Guide", "category": "unknown", "content": "Sample content"},
    )
    assert resp_err4.status_code == 400
    assert "Category must be one of" in resp_err4.get_json()["error"]

    # 2. Valid Create
    payload = {
        "title": "Study Guide Generator",
        "category": "study",
        "blurb": "Creates a structured revision outline",
        "content": "You are a master educator. Create a comprehensive revision outline for: {topic}",
    }
    resp_create = admin_client.post("/api/templates", json=payload)
    assert resp_create.status_code == 201
    created_tpl = resp_create.get_json()
    assert created_tpl["title"] == payload["title"]
    assert created_tpl["category"] == payload["category"]
    assert created_tpl["blurb"] == payload["blurb"]
    assert created_tpl["content"] == payload["content"]
    tpl_id = created_tpl["id"]

    # Check audit log recorded template_create
    audit_logs = get_recent_admin_audit(10, db_path=db_path)
    assert any(log["action"] == "template_create" and str(tpl_id) in (log["details"] or "") for log in audit_logs)

    # 3. Read template
    resp_get = admin_client.get(f"/api/templates/{tpl_id}")
    assert resp_get.status_code == 200
    assert resp_get.get_json()["content"] == payload["content"]

    resp_list = admin_client.get("/api/templates")
    assert resp_list.status_code == 200
    templates_list = resp_list.get_json()
    assert len(templates_list) == 1
    assert templates_list[0]["id"] == tpl_id
    assert templates_list[0]["title"] == payload["title"]

    # 4. Validation on update
    resp_up_err = admin_client.put(
        f"/api/templates/{tpl_id}",
        json={"title": "", "category": "study", "content": "new content"},
    )
    assert resp_up_err.status_code == 400

    resp_up_cat_err = admin_client.put(
        f"/api/templates/{tpl_id}",
        json={"title": "Updated", "category": "coding", "content": "new content"},
    )
    assert resp_up_cat_err.status_code == 400

    # 5. Valid Update
    update_payload = {
        "title": "Advanced Study Guide Generator",
        "category": "research",
        "blurb": "Creates deep-dive research notes and questions",
        "content": "You are a research mentor. Formulate deep-dive notes for: {topic}",
    }
    resp_update = admin_client.put(f"/api/templates/{tpl_id}", json=update_payload)
    assert resp_update.status_code == 200
    updated_tpl = resp_update.get_json()
    assert updated_tpl["title"] == update_payload["title"]
    assert updated_tpl["category"] == update_payload["category"]

    # Check audit log recorded template_edit
    audit_logs = get_recent_admin_audit(10, db_path=db_path)
    assert any(log["action"] == "template_edit" and str(tpl_id) in (log["details"] or "") for log in audit_logs)

    # 6. Delete template
    resp_del = admin_client.delete(f"/api/templates/{tpl_id}")
    assert resp_del.status_code == 200
    assert resp_del.get_json() == {"ok": True, "message": "Template deleted successfully"}

    # Verify deleted
    resp_get_deleted = admin_client.get(f"/api/templates/{tpl_id}")
    assert resp_get_deleted.status_code == 404

    # Check audit log recorded template_delete
    audit_logs = get_recent_admin_audit(10, db_path=db_path)
    assert any(log["action"] == "template_delete" and str(tpl_id) in (log["details"] or "") for log in audit_logs)


def test_non_admin_gets_404_on_crud_routes(guest_client: FlaskClient, auth_client: FlaskClient, tpl_app) -> None:
    """Verify unauthenticated users, regular users, and non-gated admins receive 404 on mutating template routes."""
    db_path = tpl_app.config["DB_PATH"]
    # Seed a template directly via db helper to test edit/delete
    tpl_id = create_template("Seed", "study", "Blurb", "Content", db_path=db_path)

    # 1. Guest client
    assert guest_client.post("/api/templates", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert guest_client.put(f"/api/templates/{tpl_id}", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert guest_client.delete(f"/api/templates/{tpl_id}").status_code == 404

    # 2. Authenticated non-admin client
    assert auth_client.post("/api/templates", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert auth_client.put(f"/api/templates/{tpl_id}", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert auth_client.delete(f"/api/templates/{tpl_id}").status_code == 404

    # 3. Admin user without gate pass
    ungated_client = tpl_app.test_client()
    ungated_client.post("/admin/login", json={"username": "super_admin", "password": "admin_pass123"})
    assert ungated_client.post("/api/templates", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert ungated_client.put(f"/api/templates/{tpl_id}", json={"title": "A", "category": "study", "content": "B"}).status_code == 404
    assert ungated_client.delete(f"/api/templates/{tpl_id}").status_code == 404


def test_use_template_creates_ready_session_no_llm_call(auth_client: FlaskClient, tpl_app) -> None:
    """Verify using a template creates a session with status='ready' and saves the prompt without any LLM calls."""
    db_path = tpl_app.config["DB_PATH"]
    tpl_id = create_template(
        "Essay Outliner",
        "writing",
        "Outlines a 5-paragraph argumentative essay",
        "Write a 5-paragraph essay analyzing: {thesis}",
        db_path=db_path,
    )

    with patch("services.llm.call_llm") as mock_call_llm:
        # 1. Use template via /api/templates/<id>/use
        resp = auth_client.post(f"/api/templates/{tpl_id}/use")
        assert resp.status_code == 201
        data = resp.get_json()
        assert "session_id" in data
        assert data["status"] == "ready"
        session_id = data["session_id"]

        # Ensure call_llm was never invoked!
        mock_call_llm.assert_not_called()

        # Verify session state from API
        sess_resp = auth_client.get(f"/api/sessions/{session_id}")
        assert sess_resp.status_code == 200
        sess_data = sess_resp.get_json()
        assert sess_data["status"] == "ready"
        assert sess_data["type"] == "writing"
        assert sess_data["final_prompt"] == "Write a 5-paragraph essay analyzing: {thesis}"
        assert len(sess_data["prompts"]) >= 1
        assert sess_data["prompts"][-1] == "Write a 5-paragraph essay analyzing: {thesis}"

        # 2. Use template via POST /api/sessions with template_id
        resp2 = auth_client.post("/api/sessions", json={"template_id": tpl_id})
        assert resp2.status_code == 201
        data2 = resp2.get_json()
        assert data2["status"] == "ready"
        assert data2["final_prompt"] == "Write a 5-paragraph essay analyzing: {thesis}"

        # Verify LLM call was still never called!
        mock_call_llm.assert_not_called()

    # 3. Using nonexistent template returns 404
    resp_nf = auth_client.post("/api/templates/99999/use")
    assert resp_nf.status_code == 404
    assert resp_nf.get_json() == {"error": "Template not found"}
