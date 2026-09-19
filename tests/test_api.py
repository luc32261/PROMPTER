import json
from pathlib import Path
from unittest.mock import patch
import pytest
from flask.testing import FlaskClient

from app import create_app
from db import create_user
from services.builder import ModelOutputError


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    """Fixture providing a test client configured with an isolated test database."""
    test_db = tmp_path / "test_api.db"
    test_app = create_app(db_path=test_db)
    test_app.config["TESTING"] = True
    test_app.config["RATELIMIT_ENABLED"] = False
    user_id = create_user("testuser", "mock_hash", db_path=test_db)
    test_client = test_app.test_client()
    with test_client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["user_id"] = user_id
        sess["username"] = "testuser"
    return test_client


def test_health_route(client: FlaskClient) -> None:
    """Verify GET /api/health returns 200 and ok=true."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_create_session_success(client: FlaskClient) -> None:
    """Verify POST /api/sessions successfully creates a session and returns initial question."""
    mock_reply = json.dumps({
        "status": "ask",
        "type": "study",
        "question": "What level of calculus is this for?",
    })
    with patch("services.builder.call_llm", return_value=mock_reply):
        resp = client.post("/api/sessions", json={"idea": "Help me study calculus"})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["session_id"] == 1
        assert data["status"] == "ask"
        assert data["type"] == "study"
        assert data["question"] == "What level of calculus is this for?"


def test_create_session_validation_errors(client: FlaskClient) -> None:
    """Verify POST /api/sessions validates missing, empty, invalid, and overly long ideas."""
    # Invalid JSON
    resp = client.post("/api/sessions", data="not json", content_type="application/json")
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    # Empty idea
    resp = client.post("/api/sessions", json={"idea": "   "})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Idea must be a non-empty string"}

    # Missing idea field
    resp = client.post("/api/sessions", json={"other": "value"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Idea must be a non-empty string"}

    # Idea exceeding 4000 characters
    long_idea = "a" * 4001
    resp = client.post("/api/sessions", json={"idea": long_idea})
    assert resp.status_code == 400
    assert "4000 characters or less" in resp.get_json()["error"]


def test_create_session_model_error(client: FlaskClient) -> None:
    """Verify POST /api/sessions returns 502 when LLM returns invalid output twice."""
    with patch("services.builder.call_llm", return_value="invalid json"):
        resp = client.post("/api/sessions", json={"idea": "Valid idea"})
        assert resp.status_code == 502
        assert resp.get_json() == {"error": "Model returned invalid output"}


def test_send_message_success_and_skip(client: FlaskClient) -> None:
    """Verify POST /api/sessions/<id>/messages advances session and supports skip=True."""
    ask_reply = json.dumps({
        "status": "ask",
        "type": "writing",
        "question": "What is the tone?",
    })
    ready_reply = json.dumps({
        "status": "ready",
        "type": "writing",
        "final_prompt": "## Role\nWriter\n## Task\nDraft letter.",
    })

    with patch("services.builder.call_llm", return_value=ask_reply):
        create_resp = client.post("/api/sessions", json={"idea": "Write an email"})
        session_id = create_resp.get_json()["session_id"]

    # Normal answer
    with patch("services.builder.call_llm", return_value=ask_reply):
        resp = client.post(f"/api/sessions/{session_id}/messages", json={"text": "Formal tone"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ask"

    # Skip questions
    with patch("services.builder.call_llm", return_value=ready_reply):
        resp = client.post(f"/api/sessions/{session_id}/messages", json={"skip": True})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ready"
        assert "final_prompt" in data


def test_send_message_validation_errors(client: FlaskClient) -> None:
    """Verify POST /api/sessions/<id>/messages returns 400 for empty text and 404 for unknown session."""
    # Unknown session
    resp = client.post("/api/sessions/999/messages", json={"text": "Hello"})
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Session not found"}

    # Create a valid session first
    ask_reply = json.dumps({"status": "ask", "type": "study", "question": "Question?"})
    with patch("services.builder.call_llm", return_value=ask_reply):
        create_resp = client.post("/api/sessions", json={"idea": "Study topic"})
        session_id = create_resp.get_json()["session_id"]

    # Empty text without skip
    resp = client.post(f"/api/sessions/{session_id}/messages", json={"text": "   ", "skip": False})
    assert resp.status_code == 400
    assert "Text must be a non-empty string" in resp.get_json()["error"]

    # Over 4000 characters
    resp = client.post(f"/api/sessions/{session_id}/messages", json={"text": "a" * 4001})
    assert resp.status_code == 400
    assert "4000 characters or less" in resp.get_json()["error"]


def test_list_and_get_sessions(client: FlaskClient) -> None:
    """Verify GET /api/sessions and GET /api/sessions/<id>."""
    ask_reply = json.dumps({"status": "ask", "type": "research", "question": "Scope?"})
    ready_reply = json.dumps({
        "status": "ready",
        "type": "research",
        "final_prompt": "## Role\nResearcher",
    })

    with patch("services.builder.call_llm", return_value=ask_reply):
        s1 = client.post("/api/sessions", json={"idea": "First session"}).get_json()["session_id"]
        s2 = client.post("/api/sessions", json={"idea": "Second session"}).get_json()["session_id"]

    # List sessions - newest first
    list_resp = client.get("/api/sessions")
    assert list_resp.status_code == 200
    sessions = list_resp.get_json()
    assert len(sessions) == 2
    assert sessions[0]["id"] == s2
    assert sessions[1]["id"] == s1

    # Get single session detail
    detail_resp = client.get(f"/api/sessions/{s1}")
    assert detail_resp.status_code == 200
    detail = detail_resp.get_json()
    assert detail["id"] == s1
    assert detail["idea"] == "First session"
    assert len(detail["messages"]) == 2
    assert detail["final_prompt"] is None

    # Unknown session detail
    assert client.get("/api/sessions/999").status_code == 404


def test_delete_session(client: FlaskClient) -> None:
    """Verify DELETE /api/sessions/<id> deletes the session and subsequent requests return 404."""
    ask_reply = json.dumps({"status": "ask", "type": "other", "question": "Question?"})
    with patch("services.builder.call_llm", return_value=ask_reply):
        s_id = client.post("/api/sessions", json={"idea": "To delete"}).get_json()["session_id"]

    del_resp = client.delete(f"/api/sessions/{s_id}")
    assert del_resp.status_code == 200
    assert del_resp.get_json() == {"ok": True}

    # Verify session is gone
    assert client.get(f"/api/sessions/{s_id}").status_code == 404

    # Deleting again returns 404
    assert client.delete(f"/api/sessions/{s_id}").status_code == 404


def test_refine_prompt_success(client: FlaskClient) -> None:
    """Verify POST /api/sessions/<id>/refine generates shorter and detailed variants."""
    ready_reply = json.dumps({
        "status": "ready",
        "type": "study",
        "final_prompt": "## Role\nOriginal Prompt",
    })
    with patch("services.builder.call_llm", return_value=ready_reply):
        s_id = client.post("/api/sessions", json={"idea": "Study calculus"}).get_json()["session_id"]

    # Refine shorter
    shorter_reply = "## Role\nShorter Prompt"
    with patch("services.builder.call_llm", return_value=shorter_reply) as mock_llm:
        resp = client.post(f"/api/sessions/{s_id}/refine", json={"mode": "shorter"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["final_prompt"] == shorter_reply
        assert data["version"] == 2
        assert len(data["prompts"]) == 2

        # Verify call_llm had json_mode=False
        _, kwargs = mock_llm.call_args
        assert kwargs.get("json_mode") is False

    # Refine detailed
    detailed_reply = "## Role\nDetailed Prompt with extra examples"
    with patch("services.builder.call_llm", return_value=detailed_reply):
        resp = client.post(f"/api/sessions/{s_id}/refine", json={"mode": "detailed"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["final_prompt"] == detailed_reply
        assert data["version"] == 3
        assert len(data["prompts"]) == 3

    # Check that GET /api/sessions/<id> returns all 3 versions and latest prompt
    detail_resp = client.get(f"/api/sessions/{s_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.get_json()
    assert detail["final_prompt"] == detailed_reply
    assert len(detail["prompts"]) == 3


def test_refine_prompt_validation(client: FlaskClient) -> None:
    """Verify validation for POST /api/sessions/<id>/refine."""
    # Unknown session -> 404
    resp = client.post("/api/sessions/999/refine", json={"mode": "shorter"})
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Session not found"}

    # Session with no prompt yet (asking state) -> 400
    ask_reply = json.dumps({"status": "ask", "type": "study", "question": "Level?"})
    with patch("services.builder.call_llm", return_value=ask_reply):
        s_id = client.post("/api/sessions", json={"idea": "Study calculus"}).get_json()["session_id"]

    resp = client.post(f"/api/sessions/{s_id}/refine", json={"mode": "shorter"})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Session has no prompt to refine"}

    # Invalid mode -> 400
    ready_reply = json.dumps({"status": "ready", "type": "study", "final_prompt": "## Role\nPrompt"})
    with patch("services.builder.call_llm", return_value=ready_reply):
        client.post(f"/api/sessions/{s_id}/messages", json={"skip": True})

    resp = client.post(f"/api/sessions/{s_id}/refine", json={"mode": "invalid_mode"})
    assert resp.status_code == 400
    assert "Mode must be 'shorter' or 'detailed'" in resp.get_json()["error"]


def test_improve_prompt_success(client: FlaskClient) -> None:
    """Verify POST /api/improve returns improved prompt and changes, saving as session."""
    mock_reply = json.dumps({
        "final_prompt": "## Role\nExpert Poet\n## Task\nCompose a sonnet about rescue dogs.",
        "changes": ["Added explicit poetic form (sonnet)", "Specified subject depth"],
    })
    with patch("services.builder.call_llm", return_value=mock_reply):
        resp = client.post("/api/improve", json={"prompt": "write a poem about rescue dogs"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ready"
        assert "Expert Poet" in data["final_prompt"]
        assert len(data["changes"]) == 2
        session_id = data["session_id"]

        # Check it is listed in sessions
        list_resp = client.get("/api/sessions")
        assert any(s["id"] == session_id for s in list_resp.get_json())


def test_improve_prompt_validation(client: FlaskClient) -> None:
    """Verify input validation for POST /api/improve."""
    # Empty prompt
    resp = client.post("/api/improve", json={"prompt": "   "})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Prompt must be a non-empty string"}

    # Over 4000 chars
    resp = client.post("/api/improve", json={"prompt": "a" * 4001})
    assert resp.status_code == 400
    assert "4000 characters or less" in resp.get_json()["error"]


def test_score_prompt_success(client: FlaskClient) -> None:
    """Verify POST /api/sessions/<id>/score returns 1-10 scores and suggestions."""
    ready_reply = json.dumps({
        "status": "ready",
        "type": "study",
        "final_prompt": "## Role\nTutor\n## Task\nTeach calculus",
    })
    with patch("services.builder.call_llm", return_value=ready_reply):
        s_id = client.post("/api/sessions", json={"idea": "Calculus"}).get_json()["session_id"]

    score_reply = json.dumps({
        "clarity": 9,
        "specificity": 8,
        "completeness": 9,
        "overall": 9,
        "suggestions": ["Add length constraint", "Specify calculator policy"],
    })
    with patch("services.builder.call_llm", return_value=score_reply):
        resp = client.post(f"/api/sessions/{s_id}/score")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["clarity"] == 9
        assert data["overall"] == 9
        assert len(data["suggestions"]) == 2


def test_score_prompt_validation(client: FlaskClient) -> None:
    """Verify validation for POST /api/sessions/<id>/score."""
    # Unknown session -> 404
    resp = client.post("/api/sessions/999/score")
    assert resp.status_code == 404

    # Session in asking state with no prompt -> 400
    ask_reply = json.dumps({"status": "ask", "type": "study", "question": "Question?"})
    with patch("services.builder.call_llm", return_value=ask_reply):
        s_id = client.post("/api/sessions", json={"idea": "Topic"}).get_json()["session_id"]

    resp = client.post(f"/api/sessions/{s_id}/score")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Session has no prompt to score"}


