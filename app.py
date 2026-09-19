import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from db import (
    create_user,
    delete_session,
    get_all_prompts,
    get_final_prompt,
    get_messages,
    get_session,
    get_user_by_id,
    get_user_by_username,
    init_db,
    list_sessions,
)
from services.builder import (
    ModelOutputError,
    advance_session,
    create_session_with_idea,
    improve_prompt,
    refine_prompt,
    score_prompt,
)

load_dotenv()

MAX_INPUT_LENGTH = 4000


def create_app(
    db_path: str | Path | None = None,
    test_config: dict[str, Any] | None = None,
) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    app.config["DEBUG"] = False
    app.config["DB_PATH"] = db_path

    if test_config:
        app.config.update(test_config)

    # Initialize rate limiter
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=app.config.get("RATELIMIT_STORAGE_URI", "memory://"),
    )

    # Ensure database schema is initialized
    init_db(db_path)

    @app.errorhandler(429)
    def handle_ratelimit_exceeded(err: Any) -> tuple[Response, int]:
        """Handle 429 rate limit exceeded error with consistent JSON error shape."""
        return jsonify({"error": "Rate limit exceeded"}), 429

    @app.errorhandler(ModelOutputError)
    def handle_model_output_error(err: ModelOutputError) -> tuple[Response, int]:
        """Handle LLM invalid output error by returning 502 with consistent shape."""
        return jsonify({"error": str(err)}), 502

    @app.errorhandler(HTTPException)
    def handle_http_exception(err: HTTPException) -> tuple[Response, int]:
        """Handle standard HTTP exceptions with consistent JSON error shape."""
        code = err.code if err.code is not None else 500
        desc = err.description if err.description else "Internal Server Error"
        return jsonify({"error": desc}), code

    @app.errorhandler(Exception)
    def handle_unexpected_exception(err: Exception) -> tuple[Response, int]:
        """Handle unexpected server exceptions."""
        return jsonify({"error": "Internal Server Error"}), 500

    @app.before_request
    def require_login() -> Response | None:
        """Protect all /api routes and the main page with session authentication."""
        # Whitelisted endpoints
        if request.path in {"/login", "/register", "/logout"} or request.path.startswith("/static/"):
            return None

        if not session.get("authenticated") or not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return None

    @app.route("/register", methods=["GET", "POST"])
    def register_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Render register page or register a new user account."""
        if request.method == "GET":
            if session.get("authenticated") and session.get("user_id"):
                return redirect(url_for("index"))
            return render_template("register.html")

        # Read credentials (JSON or Form)
        username = None
        password = None
        confirm_password = None
        if request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                username = data.get("username")
                password = data.get("password")
                confirm_password = data.get("confirm_password")
        else:
            username = request.form.get("username")
            password = request.form.get("password")
            confirm_password = request.form.get("confirm_password")

        def fail(msg: str) -> tuple[Response, int] | tuple[str, int]:
            if request.is_json:
                return jsonify({"error": msg}), 400
            return render_template("register.html", error=msg, username=username or ""), 400

        if not isinstance(username, str) or not username.strip():
            return fail("Username is required")

        username = username.strip()
        if len(username) < 3 or len(username) > 30:
            return fail("Username must be between 3 and 30 characters")

        if not username.replace("_", "").isalnum():
            return fail("Username may only contain letters, numbers, and underscores")

        if not isinstance(password, str) or len(password) < 6:
            return fail("Password must be at least 6 characters")

        if confirm_password is not None and password != confirm_password:
            return fail("Passwords do not match")

        current_db = app.config.get("DB_PATH")
        existing_user = get_user_by_username(username, db_path=current_db)
        if existing_user:
            return fail("Username is already taken")

        password_hash = generate_password_hash(password)
        user_id = create_user(username, password_hash, db_path=current_db)
        if user_id is None:
            return fail("Could not create account, please try again")

        session["authenticated"] = True
        session["user_id"] = user_id
        session["username"] = username

        if request.is_json:
            return jsonify({"ok": True, "user_id": user_id, "username": username}), 201
        return redirect(url_for("index"))

    @app.route("/login", methods=["GET", "POST"])
    def login_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Render login page on GET or verify username and password on POST."""
        if request.method == "GET":
            if session.get("authenticated") and session.get("user_id"):
                return redirect(url_for("index"))
            return render_template("login.html")

        # Handle POST credentials (JSON or Form)
        username = None
        password = None
        if request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                username = data.get("username")
                password = data.get("password")
        else:
            username = request.form.get("username")
            password = request.form.get("password")

        def fail(msg: str) -> tuple[Response, int] | tuple[str, int]:
            if request.is_json:
                return jsonify({"error": msg}), 401
            return render_template("login.html", error=msg, username=username or ""), 401

        if not isinstance(username, str) or not username.strip():
            return fail("Username is required")

        if not isinstance(password, str) or not password:
            return fail("Password is required")

        current_db = app.config.get("DB_PATH")
        user = get_user_by_username(username.strip(), db_path=current_db)
        if not user or not check_password_hash(user["password_hash"], password):
            return fail("Invalid username or password")

        session["authenticated"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]

        if request.is_json:
            return jsonify({"ok": True, "user_id": user["id"], "username": user["username"]}), 200
        return redirect(url_for("index"))

    @app.route("/logout", methods=["GET", "POST"])
    def logout() -> Response:
        """Clear user session and redirect to login page."""
        session.clear()
        return redirect(url_for("login_page"))

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the single-page frontend application."""
        return render_template("index.html", username=session.get("username", "User"))

    @app.route("/api/health", methods=["GET"])
    def health_check() -> Response:
        """Health check route to verify service availability."""
        return jsonify({"ok": True})

    @app.route("/api/sessions", methods=["POST"])
    @limiter.limit("20 per minute")
    def create_session_route() -> tuple[Response, int]:
        """Start a new prompt session from an initial user idea."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        idea = data.get("idea")
        if not isinstance(idea, str) or not idea.strip():
            return jsonify({"error": "Idea must be a non-empty string"}), 400

        if len(idea) > MAX_INPUT_LENGTH:
            return jsonify({"error": f"Input must be {MAX_INPUT_LENGTH} characters or less"}), 400

        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        result = create_session_with_idea(idea.strip(), user_id=user_id, db_path=current_db)
        return jsonify(result), 201

    @app.route("/api/sessions/<int:session_id>/messages", methods=["POST"])
    @limiter.limit("20 per minute")
    def send_message_route(session_id: int) -> tuple[Response, int]:
        """Advance a session with user response or skip instruction."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        skip = bool(data.get("skip", False))
        text = data.get("text", "")

        if not skip:
            if not isinstance(text, str) or not text.strip():
                return jsonify({"error": "Text must be a non-empty string when not skipping"}), 400

        if isinstance(text, str) and len(text) > MAX_INPUT_LENGTH:
            return jsonify({"error": f"Input must be {MAX_INPUT_LENGTH} characters or less"}), 400

        cleaned_text = text.strip() if isinstance(text, str) else ""
        result = advance_session(session_id, text=cleaned_text, skip=skip, db_path=current_db)
        return jsonify(result), 200

    @app.route("/api/sessions", methods=["GET"])
    def list_sessions_route() -> Response:
        """List all prompt sessions belonging to the current user, newest first."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        rows = list_sessions(user_id=user_id, db_path=current_db)
        sessions_list: list[dict[str, Any]] = [
            {
                "id": r["id"],
                "idea": r["idea"],
                "type": r["type"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return jsonify(sessions_list)

    @app.route("/api/sessions/<int:session_id>", methods=["GET"])
    def get_session_route(session_id: int) -> tuple[Response, int]:
        """Get session details, message history, and final prompt."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        messages = get_messages(session_id, db_path=current_db)
        prompt_row = get_final_prompt(session_id, db_path=current_db)
        all_prompts = get_all_prompts(session_id, db_path=current_db)

        response_data: dict[str, Any] = {
            "id": session_row["id"],
            "idea": session_row["idea"],
            "type": session_row["type"],
            "status": session_row["status"],
            "created_at": session_row["created_at"],
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                }
                for m in messages
            ],
            "final_prompt": prompt_row["final_prompt"] if prompt_row else None,
            "prompts": [p["final_prompt"] for p in all_prompts],
        }
        return jsonify(response_data), 200

    @app.route("/api/sessions/<int:session_id>/refine", methods=["POST"])
    @limiter.limit("20 per minute")
    def refine_prompt_route(session_id: int) -> tuple[Response, int]:
        """Refine the final prompt to be shorter or more detailed."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        mode = data.get("mode")
        if mode not in {"shorter", "detailed"}:
            return jsonify({"error": "Mode must be 'shorter' or 'detailed'"}), 400

        try:
            result = refine_prompt(session_id, mode=mode, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    @app.route("/api/improve", methods=["POST"])
    @limiter.limit("20 per minute")
    def improve_prompt_route() -> tuple[Response, int]:
        """Improve an existing user prompt and return the improved version with changes."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"error": "Prompt must be a non-empty string"}), 400

        if len(prompt) > MAX_INPUT_LENGTH:
            return jsonify({"error": f"Input must be {MAX_INPUT_LENGTH} characters or less"}), 400

        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        try:
            result = improve_prompt(prompt.strip(), user_id=user_id, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    @app.route("/api/sessions/<int:session_id>/score", methods=["POST"])
    @limiter.limit("20 per minute")
    def score_prompt_route(session_id: int) -> tuple[Response, int]:
        """Score the latest final prompt of a session on clarity, specificity, and completeness."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        try:
            result = score_prompt(session_id, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    @app.route("/api/sessions/<int:session_id>", methods=["DELETE"])
    def delete_session_route(session_id: int) -> tuple[Response, int]:
        """Delete a session and its associated messages and prompts."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        delete_session(session_id, user_id=user_id, db_path=current_db)
        return jsonify({"ok": True}), 200

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=False)
