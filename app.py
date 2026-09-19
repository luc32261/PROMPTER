from pathlib import Path
from typing import Any
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from db import (
    delete_session,
    get_all_prompts,
    get_final_prompt,
    get_messages,
    get_session,
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

MAX_INPUT_LENGTH = 4000


def create_app(db_path: str | Path | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path

    # Ensure database schema is initialized
    init_db(db_path)

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the single-page frontend application."""
        return render_template("index.html")

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

    @app.route("/api/health", methods=["GET"])
    def health_check() -> Response:
        """Health check route to verify service availability."""
        return jsonify({"ok": True})

    @app.route("/api/sessions", methods=["POST"])
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
        result = create_session_with_idea(idea.strip(), db_path=current_db)
        return jsonify(result), 201

    @app.route("/api/sessions/<int:session_id>/messages", methods=["POST"])
    def send_message_route(session_id: int) -> tuple[Response, int]:
        """Advance a session with user response or skip instruction."""
        current_db = app.config.get("DB_PATH")
        session = get_session(session_id, db_path=current_db)
        if session is None:
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
        """List all prompt sessions newest first."""
        current_db = app.config.get("DB_PATH")
        rows = list_sessions(db_path=current_db)
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
        session = get_session(session_id, db_path=current_db)
        if session is None:
            return jsonify({"error": "Session not found"}), 404

        messages = get_messages(session_id, db_path=current_db)
        prompt_row = get_final_prompt(session_id, db_path=current_db)
        all_prompts = get_all_prompts(session_id, db_path=current_db)

        response_data: dict[str, Any] = {
            "id": session["id"],
            "idea": session["idea"],
            "type": session["type"],
            "status": session["status"],
            "created_at": session["created_at"],
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
    def refine_prompt_route(session_id: int) -> tuple[Response, int]:
        """Refine the final prompt to be shorter or more detailed."""
        current_db = app.config.get("DB_PATH")
        session = get_session(session_id, db_path=current_db)
        if session is None:
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
        try:
            result = improve_prompt(prompt.strip(), db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    @app.route("/api/sessions/<int:session_id>/score", methods=["POST"])
    def score_prompt_route(session_id: int) -> tuple[Response, int]:
        """Score the latest final prompt of a session on clarity, specificity, and completeness."""
        current_db = app.config.get("DB_PATH")
        session = get_session(session_id, db_path=current_db)
        if session is None:
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
        session = get_session(session_id, db_path=current_db)
        if session is None:
            return jsonify({"error": "Session not found"}), 404

        delete_session(session_id, db_path=current_db)
        return jsonify({"ok": True}), 200

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=True)
