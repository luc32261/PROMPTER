import hmac
import os
import secrets
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from flask import Flask, Response, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from db import (
    clear_admin_audit,
    clear_failed_attempts,
    create_session_from_template,
    create_template,
    create_user,
    delete_session,
    delete_template,
    delete_user_cascade,
    get_all_prompts,
    get_failed_attempts_count,
    get_final_prompt,
    get_messages,
    get_recent_admin_audit,
    get_session,
    get_template_by_id,
    get_total_usage_today,
    get_user_by_id,
    get_user_by_username,
    get_user_usage_today,
    increment_user_session_version,
    init_db,
    list_sessions,
    list_templates,
    list_users_with_stats,
    record_admin_audit,
    record_failed_attempt,
    record_llm_usage,
    reset_user_password,
    set_user_daily_limit,
    sync_admin,
    toggle_user_active,
    update_template,
    update_user_login,
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
ALLOWED_TEMPLATE_CATEGORIES = {"study", "writing", "research", "other"}


def check_and_enforce_daily_limit(user_id: int | None, db_path: str | Path | None = None) -> tuple[Response, int] | None:
    """Check if the user has reached their daily LLM limit for the current UTC day. Admin is exempt."""
    if not user_id:
        return None
    user = get_user_by_id(user_id, db_path=db_path)
    if not user or user["role"] == "admin":
        return None

    # Fallback to DAILY_LIMIT env var, default 100
    env_default = 100
    env_limit_str = os.getenv("DAILY_LIMIT", "100").strip()
    if env_limit_str.isdigit():
        env_default = int(env_limit_str)

    effective_limit = user["daily_limit"] if user["daily_limit"] is not None else env_default

    current_usage = get_user_usage_today(user_id, db_path=db_path)
    if current_usage >= effective_limit:
        return jsonify({"error": "Daily limit reached"}), 429
    return None


def admin_required(f: Any) -> Any:
    """Ensure the user is authenticated, has the 'admin' role, and valid admin_until, otherwise return 404."""
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        user_id = session.get("user_id")
        admin_until = session.get("admin_until", 0)
        if not session.get("authenticated") or not user_id:
            return jsonify({"error": "Not Found"}), 404

        if not isinstance(admin_until, (int, float)) or admin_until <= time.time():
            return jsonify({"error": "Not Found"}), 404

        current_db = current_app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user or user["is_active"] == 0 or user["role"] != "admin":
            return jsonify({"error": "Not Found"}), 404
        return f(*args, **kwargs)
    return decorated_function


def get_limiter_key() -> str:
    """Rate limit key function using user_id if authenticated, falling back to IP."""
    user_id = session.get("user_id")
    if user_id:
        return f"user:{user_id}"
    return get_remote_address()


def create_app(
    db_path: str | Path | None = None,
    test_config: dict[str, Any] | None = None,
) -> Flask:
    """Create and configure the Flask application."""
    is_prod = (
        os.getenv("FLASK_ENV", "").lower() == "production"
        or os.getenv("ENV", "").lower() == "production"
        or os.getenv("ENVIRONMENT", "").lower() == "production"
        or (test_config and str(test_config.get("ENV", "")).lower() == "production")
        or (test_config and str(test_config.get("FLASK_ENV", "")).lower() == "production")
    )
    if is_prod:
        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
        admin_gate_password = os.getenv("ADMIN_GATE_PASSWORD", "").strip()
        if not admin_username or not admin_password or not admin_gate_password:
            raise RuntimeError(
                "ADMIN_USERNAME, ADMIN_PASSWORD, and ADMIN_GATE_PASSWORD environment variables are required in production."
            )

    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or "dev-secret-key-change-in-production"
    app.permanent_session_lifetime = timedelta(days=30)
    app.config["DEBUG"] = False
    app.config["DB_PATH"] = db_path
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = is_prod
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    if test_config:
        app.config.update(test_config)

    @app.after_request
    def add_static_cache_control(response: Response) -> Response:
        """Prevent mobile browsers from caching static assets aggressively."""
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.context_processor
    def inject_csrf_token() -> dict[str, Any]:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return {"csrf_token": session["csrf_token"]}

    # Initialize rate limiter
    limiter = Limiter(
        get_limiter_key,
        app=app,
        default_limits=[],
        storage_uri=app.config.get("RATELIMIT_STORAGE_URI", "memory://"),
    )

    # Ensure database schema is initialized and single admin is synced
    init_db(db_path)
    sync_admin(db_path)

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
        app.logger.error(f"Unhandled exception: {err}", exc_info=True)
        return jsonify({"error": str(err) or "Internal Server Error"}), 500

    @app.before_request
    def require_login() -> Response | None:
        """Handle CSRF validation and session authentication for protected routes."""
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)

        # CSRF protection for all POST, PUT, PATCH, DELETE requests
        if app.config.get("CSRF_ENABLED", True) and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            token = (
                request.headers.get("X-CSRF-Token")
                or request.form.get("csrf_token")
                or (
                    request.is_json
                    and isinstance(request.get_json(silent=True), dict)
                    and request.get_json(silent=True).get("csrf_token")
                )
            )
            expected = session.get("csrf_token")
            if not expected or not token or not hmac.compare_digest(str(token), str(expected)):
                return jsonify({"error": "CSRF token missing or invalid"}), 403

        # Whitelisted endpoints
        if (
            request.path in {"/login", "/register", "/logout", "/admin/gate", "/admin/login", "/admin/logout"}
            or request.path.startswith("/static/")
            or (app.config.get("TESTING") and request.path.startswith("/test-"))
            or (request.method == "GET" and (request.path == "/api/templates" or request.path.startswith("/api/templates/")))
        ):
            return None

        user_id = session.get("user_id")
        if not session.get("authenticated") or not user_id:
            if (
                request.path == "/admin"
                or request.path.startswith("/admin/")
                or request.path.startswith("/api/admin/")
                or (
                    request.method in {"POST", "PUT", "DELETE"}
                    and (
                        request.path == "/api/templates"
                        or (request.path.startswith("/api/templates/") and not request.path.endswith("/use"))
                    )
                )
            ):
                return jsonify({"error": "Not Found"}), 404
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))

        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))

        if user["is_active"] == 0:
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "account disabled"}), 401
            return render_template("login.html", error="account disabled"), 401

        session_ver = session.get("session_version", 0)
        if session_ver != user["session_version"]:
            session.clear()
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "logged out by admin"}), 401
            return render_template("login.html", error="logged out by admin"), 401

        session["role"] = user["role"]
        session["username"] = user["username"]
        return None

    @app.route("/register", methods=["GET", "POST"])
    def register_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Render register page or register a new user account."""
        if request.method == "GET":
            if session.get("authenticated") and session.get("user_id"):
                current_db = app.config.get("DB_PATH")
                user = get_user_by_id(session.get("user_id"), db_path=current_db)
                if not user or user["is_active"] == 0 or session.get("session_version", 0) != user["session_version"]:
                    session.clear()
                else:
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

        # Block registering ADMIN_USERNAME (case-insensitive) with a generic message
        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        if admin_username and username.lower() == admin_username.lower():
            return fail("username unavailable")

        current_db = app.config.get("DB_PATH")
        existing_user = get_user_by_username(username, db_path=current_db)
        if existing_user:
            return fail("Username is already taken")

        password_hash = generate_password_hash(password)
        user_id = create_user(username, password_hash, role="user", db_path=current_db)
        if user_id is None:
            return fail("Could not create account, please try again")

        update_user_login(user_id, db_path=current_db)

        session.permanent = True
        session["authenticated"] = True
        session["user_id"] = user_id
        session["username"] = username
        session["role"] = "user"
        session["session_version"] = 0

        if request.is_json:
            return jsonify({"ok": True, "user_id": user_id, "username": username}), 201
        return redirect(url_for("index"))

    @app.route("/login", methods=["GET", "POST"])
    def login_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Render login page on GET or verify username and password on POST."""
        if request.method == "GET":
            if session.get("authenticated") and session.get("user_id"):
                current_db = app.config.get("DB_PATH")
                user = get_user_by_id(session.get("user_id"), db_path=current_db)
                if not user or user["is_active"] == 0 or session.get("session_version", 0) != user["session_version"]:
                    session.clear()
                else:
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

        if user["is_active"] == 0:
            return fail("account disabled")

        update_user_login(user["id"], db_path=current_db)

        session.permanent = True
        session["authenticated"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["session_version"] = user["session_version"]
        session.pop("admin_until", None)
        session.pop("gate_until", None)

        if request.is_json:
            return jsonify({"ok": True, "user_id": user["id"], "username": user["username"]}), 200
        return redirect(url_for("index"))

    @app.route("/logout", methods=["GET", "POST"])
    def logout() -> Response:
        """Clear user session and redirect to login page."""
        session.clear()
        return redirect(url_for("login_page"))

    @app.route("/admin/gate", methods=["GET", "POST"])
    def admin_gate_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Admin gate requiring ADMIN_GATE_PASSWORD."""
        current_db = app.config.get("DB_PATH")
        ip = get_remote_address()
        key_ip = f"gate_ip:{ip}"

        if request.method == "GET":
            if session.get("gate_until", 0) > time.time():
                return redirect(url_for("admin_login_page"))
            return render_template("admin_gate.html")

        # Check rate limit: 5 failed attempts per 15 minutes per IP
        if get_failed_attempts_count(key_ip, 900.0, db_path=current_db) >= 5:
            record_admin_audit("gate_failure", details=f"Rate limit exceeded (IP: {ip})", db_path=current_db)
            msg = "Too many failed attempts. Please try again in 15 minutes."
            if request.is_json:
                return jsonify({"error": msg}), 429
            return render_template("admin_gate.html", error=msg), 429

        gate_password = None
        if request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                gate_password = data.get("gate_password")
        else:
            gate_password = request.form.get("gate_password")

        admin_gate_password = os.getenv("ADMIN_GATE_PASSWORD", "").strip()
        valid = False
        if admin_gate_password and isinstance(gate_password, str) and gate_password:
            valid = hmac.compare_digest(gate_password.encode("utf-8"), admin_gate_password.encode("utf-8"))

        if not valid:
            record_failed_attempt(key_ip, db_path=current_db)
            record_admin_audit("gate_failure", details=f"Invalid gate password (IP: {ip})", db_path=current_db)
            if request.is_json:
                return jsonify({"error": "Invalid gate password"}), 401
            return render_template("admin_gate.html", error="Invalid gate password"), 401

        clear_failed_attempts(key_ip, db_path=current_db)
        session["gate_until"] = time.time() + 600  # 10 minutes
        record_admin_audit("gate_success", details=f"IP: {ip}", db_path=current_db)
        if request.is_json:
            return jsonify({"ok": True, "redirect": "/admin/login"}), 200
        return redirect(url_for("admin_login_page"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login_page() -> Response | str | tuple[Response, int] | tuple[str, int]:
        """Admin login form requiring valid gate_until and ADMIN_USERNAME credentials."""
        gate_until = session.get("gate_until", 0)
        if not isinstance(gate_until, (int, float)) or gate_until <= time.time():
            return jsonify({"error": "Not Found"}), 404

        current_db = app.config.get("DB_PATH")
        ip = get_remote_address()
        key_ip = f"admin_ip:{ip}"

        if request.method == "GET":
            return render_template("admin_login.html")

        # POST
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

        norm_user = username.strip().lower() if isinstance(username, str) else ""
        key_account = f"admin_account:{norm_user}" if norm_user else None

        # Check rate limit: 5 failed attempts per 15 minutes per IP and per target account
        if get_failed_attempts_count(key_ip, 900.0, db_path=current_db) >= 5 or (
            key_account and get_failed_attempts_count(key_account, 900.0, db_path=current_db) >= 5
        ):
            record_admin_audit(
                "admin_login_failure",
                admin_username=username.strip() if isinstance(username, str) and username.strip() else None,
                details=f"Rate limit exceeded (IP: {ip})",
                db_path=current_db,
            )
            msg = "Too many failed attempts. Please try again in 15 minutes."
            if request.is_json:
                return jsonify({"error": msg}), 429
            return render_template("admin_login.html", error=msg, username=username or ""), 429

        def fail_auth() -> tuple[Response, int] | tuple[str, int]:
            record_failed_attempt(key_ip, db_path=current_db)
            if key_account:
                record_failed_attempt(key_account, db_path=current_db)
            record_admin_audit(
                "admin_login_failure",
                admin_username=username.strip() if isinstance(username, str) and username.strip() else None,
                details=f"IP: {ip}",
                db_path=current_db,
            )
            msg = "Invalid credentials"
            if request.is_json:
                return jsonify({"error": msg}), 401
            return render_template("admin_login.html", error=msg, username=username or ""), 401

        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            return fail_auth()

        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        user = get_user_by_username(username.strip(), db_path=current_db)

        if (
            not user
            or user["role"] != "admin"
            or user["is_active"] != 1
            or not check_password_hash(user["password_hash"], password)
            or user["username"].lower() != admin_username.lower()
        ):
            return fail_auth()

        clear_failed_attempts(key_ip, db_path=current_db)
        if key_account:
            clear_failed_attempts(key_account, db_path=current_db)

        update_user_login(user["id"], db_path=current_db)
        record_admin_audit(
            "admin_login_success",
            admin_username=user["username"],
            details=f"IP: {ip}",
            db_path=current_db,
        )

        session.permanent = True
        session["authenticated"] = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["session_version"] = user["session_version"]
        session["admin_until"] = time.time() + 1800  # 30 minutes

        if request.is_json:
            return jsonify({"ok": True, "redirect": "/admin"}), 200
        return redirect(url_for("admin_page"))

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout() -> Response:
        """Clear admin_until and gate_until flags."""
        session.pop("admin_until", None)
        session.pop("gate_until", None)
        if request.is_json:
            return jsonify({"ok": True}), 200
        return redirect(url_for("login_page"))

    @app.route("/api/admin/check", methods=["GET"])
    @admin_required
    def admin_check_route() -> Response:
        """Admin check endpoint requiring admin role."""
        return jsonify({"ok": True, "admin": True})

    @app.route("/admin", methods=["GET"])
    @admin_required
    def admin_page() -> str:
        """Render the admin user management page."""
        current_db = app.config.get("DB_PATH")
        users = list_users_with_stats(db_path=current_db)
        total_calls_today = get_total_usage_today(db_path=current_db)
        default_limit = int(os.getenv("DAILY_LIMIT", 100))
        audit_logs = get_recent_admin_audit(limit=100, db_path=current_db)
        templates = list_templates(db_path=current_db)
        return render_template(
            "admin.html",
            users=users,
            total_calls_today=total_calls_today,
            default_limit=default_limit,
            audit_logs=audit_logs,
            templates=templates,
            current_user_id=session.get("user_id"),
            current_username=session.get("username", "Admin"),
        )

    @app.route("/admin/users/<int:user_id>/set-limit", methods=["POST"])
    @admin_required
    def admin_set_daily_limit(user_id: int) -> tuple[Response, int] | Response:
        """Set custom daily limit for a user (or None for default fallback)."""
        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            return jsonify({"error": "User not found"}), 404

        data = request.get_json(silent=True) if request.is_json else request.form
        daily_limit_val = data.get("daily_limit") if isinstance(data, dict) else request.form.get("daily_limit")

        limit_int: int | None = None
        if daily_limit_val is not None and str(daily_limit_val).strip() != "":
            try:
                limit_int = int(daily_limit_val)
                if limit_int < 0:
                    return jsonify({"error": "Daily limit must be 0 or greater"}), 400
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid daily limit format"}), 400

        try:
            set_user_daily_limit(user_id, limit_int, db_path=current_db)
            record_admin_audit(
                "limit_change",
                admin_username=session.get("username"),
                target_username=user["username"],
                details=f"Daily limit set to {limit_int if limit_int is not None else 'default'}",
                db_path=current_db,
            )
            return jsonify({"ok": True, "daily_limit": limit_int}), 200
        except ValueError:
            return jsonify({"error": "User not found"}), 404

    @app.route("/admin/users/<int:user_id>/toggle-status", methods=["POST"])
    @admin_required
    def admin_toggle_status(user_id: int) -> tuple[Response, int] | Response:
        """Toggle user active status. Admin cannot disable themselves."""
        if user_id == session.get("user_id"):
            return jsonify({"error": "Admins cannot disable their own account"}), 400

        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            return jsonify({"error": "User not found"}), 404

        try:
            new_status = toggle_user_active(user_id, db_path=current_db)
            action = "enable" if new_status == 1 else "disable"
            record_admin_audit(
                action,
                admin_username=session.get("username"),
                target_username=user["username"],
                details=f"User account {'enabled' if new_status == 1 else 'disabled'}",
                db_path=current_db,
            )
            return jsonify({"ok": True, "is_active": new_status}), 200
        except ValueError:
            return jsonify({"error": "User not found"}), 404

    @app.route("/admin/users/<int:user_id>/force-logout", methods=["POST"])
    @admin_required
    def admin_force_logout(user_id: int) -> tuple[Response, int] | Response:
        """Increment user's session version to log them out. Admin cannot force logout themselves."""
        if user_id == session.get("user_id"):
            return jsonify({"error": "Admins cannot force logout their own account"}), 400

        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            return jsonify({"error": "User not found"}), 404

        try:
            new_version = increment_user_session_version(user_id, db_path=current_db)
            record_admin_audit(
                "force_logout",
                admin_username=session.get("username"),
                target_username=user["username"],
                details=f"Session version incremented to {new_version}",
                db_path=current_db,
            )
            return jsonify({"ok": True, "session_version": new_version}), 200
        except ValueError:
            return jsonify({"error": "User not found"}), 404

    @app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
    @admin_required
    def admin_reset_password(user_id: int) -> tuple[Response, int] | Response:
        """Generate a temporary password, store only the hash, and bump session_version."""
        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            return jsonify({"error": "User not found"}), 404

        temp_password = secrets.token_urlsafe(10)
        password_hash = generate_password_hash(temp_password)
        reset_user_password(user_id, password_hash, db_path=current_db)

        record_admin_audit(
            "password_reset",
            admin_username=session.get("username"),
            target_username=user["username"],
            details="Temporary password generated",
            db_path=current_db,
        )

        return jsonify({"ok": True, "temporary_password": temp_password}), 200

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id: int) -> tuple[Response, int] | Response:
        """Delete user and all their sessions, messages, and prompts. Admin cannot delete themselves."""
        if user_id == session.get("user_id"):
            return jsonify({"error": "Admins cannot delete their own account"}), 400

        current_db = app.config.get("DB_PATH")
        user = get_user_by_id(user_id, db_path=current_db)
        if not user:
            return jsonify({"error": "User not found"}), 404

        target_username = user["username"]
        delete_user_cascade(user_id, db_path=current_db)

        record_admin_audit(
            "delete",
            admin_username=session.get("username"),
            target_username=target_username,
            details="User deleted cascade",
            db_path=current_db,
        )

        return jsonify({"ok": True}), 200

    @app.route("/admin/audit/clear", methods=["POST"])
    @admin_required
    def admin_clear_audit_route() -> tuple[Response, int] | Response:
        """Clear all entries from the admin audit log."""
        current_db = app.config.get("DB_PATH")
        clear_admin_audit(db_path=current_db)
        return jsonify({"ok": True}), 200

    @app.route("/", methods=["GET"])
    def index() -> str:
        """Render the single-page frontend application."""
        admin_until = session.get("admin_until", 0)
        admin_active = (
            session.get("role") == "admin"
            and isinstance(admin_until, (int, float))
            and admin_until > time.time()
        )
        return render_template(
            "index.html",
            username=session.get("username", "User"),
            role=session.get("role", "user"),
            admin_active=admin_active,
        )

    @app.route("/api/health", methods=["GET"])
    def health_check() -> Response:
        """Health check route to verify service availability."""
        return jsonify({"ok": True})

    # --- Template Routes ---
    @app.route("/api/templates", methods=["GET"])
    def get_templates_route() -> tuple[Response, int] | Response:
        """List all prompt templates (id, title, category, blurb)."""
        current_db = app.config.get("DB_PATH")
        rows = list_templates(db_path=current_db)
        items = [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "blurb": r["blurb"] or "",
            }
            for r in rows
        ]
        return jsonify(items), 200

    @app.route("/api/templates/<int:template_id>", methods=["GET"])
    def get_template_detail_route(template_id: int) -> tuple[Response, int] | Response:
        """Retrieve full details of a specific template including full content."""
        current_db = app.config.get("DB_PATH")
        tpl = get_template_by_id(template_id, db_path=current_db)
        if not tpl:
            return jsonify({"error": "Template not found"}), 404
        return jsonify({
            "id": tpl["id"],
            "title": tpl["title"],
            "category": tpl["category"],
            "blurb": tpl["blurb"] or "",
            "content": tpl["content"],
            "created_at": tpl["created_at"],
            "updated_at": tpl["updated_at"],
        }), 200

    @app.route("/api/templates", methods=["POST"])
    @admin_required
    def create_template_route() -> tuple[Response, int] | Response:
        """Create a new prompt template (admin only)."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        title = data.get("title")
        category = data.get("category")
        blurb = data.get("blurb")
        content = data.get("content")

        if not isinstance(title, str) or not title.strip():
            return jsonify({"error": "Title is required"}), 400
        if len(title.strip()) > 150:
            return jsonify({"error": "Title must be 150 characters or less"}), 400

        if not isinstance(category, str) or category.strip().lower() not in ALLOWED_TEMPLATE_CATEGORIES:
            return jsonify({"error": "Category must be one of: study, writing, research, other"}), 400

        if not isinstance(content, str) or not content.strip():
            return jsonify({"error": "Content is required"}), 400

        if blurb is not None and not isinstance(blurb, str):
            return jsonify({"error": "Blurb must be a string"}), 400
        if isinstance(blurb, str) and len(blurb.strip()) > 300:
            return jsonify({"error": "Blurb must be 300 characters or less"}), 400

        current_db = app.config.get("DB_PATH")
        template_id = create_template(
            title=title.strip(),
            category=category.strip().lower(),
            blurb=blurb.strip() if blurb else "",
            content=content.strip(),
            db_path=current_db,
        )

        record_admin_audit(
            "template_create",
            admin_username=session.get("username"),
            details=f"Created template ID {template_id}: '{title.strip()}' ({category.strip().lower()})",
            db_path=current_db,
        )

        created_tpl = get_template_by_id(template_id, db_path=current_db)
        return jsonify(dict(created_tpl)), 201

    @app.route("/api/templates/<int:template_id>", methods=["PUT"])
    @admin_required
    def update_template_route(template_id: int) -> tuple[Response, int] | Response:
        """Update an existing prompt template (admin only)."""
        current_db = app.config.get("DB_PATH")
        existing = get_template_by_id(template_id, db_path=current_db)
        if not existing:
            return jsonify({"error": "Template not found"}), 404

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        title = data.get("title")
        category = data.get("category")
        blurb = data.get("blurb")
        content = data.get("content")

        if not isinstance(title, str) or not title.strip():
            return jsonify({"error": "Title is required"}), 400
        if len(title.strip()) > 150:
            return jsonify({"error": "Title must be 150 characters or less"}), 400

        if not isinstance(category, str) or category.strip().lower() not in ALLOWED_TEMPLATE_CATEGORIES:
            return jsonify({"error": "Category must be one of: study, writing, research, other"}), 400

        if not isinstance(content, str) or not content.strip():
            return jsonify({"error": "Content is required"}), 400

        if blurb is not None and not isinstance(blurb, str):
            return jsonify({"error": "Blurb must be a string"}), 400
        if isinstance(blurb, str) and len(blurb.strip()) > 300:
            return jsonify({"error": "Blurb must be 300 characters or less"}), 400

        updated = update_template(
            template_id=template_id,
            title=title.strip(),
            category=category.strip().lower(),
            blurb=blurb.strip() if blurb else "",
            content=content.strip(),
            db_path=current_db,
        )
        if not updated:
            return jsonify({"error": "Template not found"}), 404

        record_admin_audit(
            "template_edit",
            admin_username=session.get("username"),
            details=f"Edited template ID {template_id}: '{title.strip()}' ({category.strip().lower()})",
            db_path=current_db,
        )

        updated_tpl = get_template_by_id(template_id, db_path=current_db)
        return jsonify(dict(updated_tpl)), 200

    @app.route("/api/templates/<int:template_id>", methods=["DELETE"])
    @admin_required
    def delete_template_route(template_id: int) -> tuple[Response, int] | Response:
        """Delete a prompt template (admin only)."""
        current_db = app.config.get("DB_PATH")
        existing = get_template_by_id(template_id, db_path=current_db)
        if not existing:
            return jsonify({"error": "Template not found"}), 404

        title = existing["title"]
        delete_template(template_id, db_path=current_db)

        record_admin_audit(
            "template_delete",
            admin_username=session.get("username"),
            details=f"Deleted template ID {template_id}: '{title}'",
            db_path=current_db,
        )

        return jsonify({"ok": True, "message": "Template deleted successfully"}), 200

    @app.route("/api/templates/<int:template_id>/use", methods=["POST"])
    def use_template_route(template_id: int) -> tuple[Response, int] | Response:
        """Create a session loaded with a template as a ready final prompt without LLM call."""
        user_id = session.get("user_id")
        current_db = app.config.get("DB_PATH")
        result = create_session_from_template(template_id, user_id=user_id, db_path=current_db)
        if not result:
            return jsonify({"error": "Template not found"}), 404
        return jsonify(result), 201

    @app.route("/api/sessions", methods=["POST"])
    @limiter.limit("20 per minute")
    def create_session_route() -> tuple[Response, int]:
        """Start a new prompt session from an initial user idea, template, or uploaded file."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")

        uploaded_file = None
        idea = None
        template_id = None

        if request.content_type and "multipart/form-data" in request.content_type:
            idea = request.form.get("idea")
            uploaded_file = request.files.get("file")
            template_id = request.form.get("template_id")
        elif request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "Invalid JSON body"}), 400
            idea = data.get("idea")
            template_id = data.get("template_id")
        else:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                idea = data.get("idea")
                template_id = data.get("template_id")
            elif request.form:
                idea = request.form.get("idea")
                uploaded_file = request.files.get("file")
                template_id = request.form.get("template_id")
            else:
                return jsonify({"error": "Invalid request format"}), 400

        if template_id is not None:
            try:
                tpl_id = int(template_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid template_id"}), 400
            result = create_session_from_template(tpl_id, user_id=user_id, db_path=current_db)
            if not result:
                return jsonify({"error": "Template not found"}), 404
            return jsonify(result), 201

        attachment: tuple[str, bytes] | None = None
        if uploaded_file and uploaded_file.filename:
            uploaded_file.seek(0, os.SEEK_END)
            file_size = uploaded_file.tell()
            uploaded_file.seek(0)
            if file_size > 10 * 1024 * 1024:
                return jsonify({"error": "File size exceeds 10MB limit"}), 400
            file_bytes = uploaded_file.read()
            attachment = (uploaded_file.filename, file_bytes)

        if not attachment:
            if not isinstance(idea, str) or not idea.strip():
                return jsonify({"error": "Idea must be a non-empty string"}), 400
        else:
            if not isinstance(idea, str) or not idea.strip():
                idea = f"Document attached: {attachment[0]}"

        if len(idea) > MAX_INPUT_LENGTH:
            return jsonify({"error": f"Input must be {MAX_INPUT_LENGTH} characters or less"}), 400

        limit_err = check_and_enforce_daily_limit(user_id, db_path=current_db)
        if limit_err:
            return limit_err

        try:
            result = create_session_with_idea(
                idea.strip(),
                user_id=user_id,
                attachment=attachment,
                db_path=current_db,
            )
            if user_id:
                record_llm_usage(user_id, request.path, success=1, db_path=current_db)
            return jsonify(result), 201
        except Exception:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            raise

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

        limit_err = check_and_enforce_daily_limit(user_id, db_path=current_db)
        if limit_err:
            return limit_err

        cleaned_text = text.strip() if isinstance(text, str) else ""
        try:
            result = advance_session(session_id, text=cleaned_text, skip=skip, db_path=current_db)
            if user_id:
                record_llm_usage(user_id, request.path, success=1, db_path=current_db)
            return jsonify(result), 200
        except Exception:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            raise

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
                "has_attachment": bool(r["has_attachment"]) if "has_attachment" in r.keys() else False,
                "attachment_filename": r["attachment_filename"] if "attachment_filename" in r.keys() else None,
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
            "has_attachment": bool(session_row["has_attachment"]) if "has_attachment" in session_row.keys() else False,
            "attachment_filename": session_row["attachment_filename"] if "attachment_filename" in session_row.keys() else None,
            "attachment_summarized": bool(session_row["attachment_summarized"]) if "attachment_summarized" in session_row.keys() else False,
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

        limit_err = check_and_enforce_daily_limit(user_id, db_path=current_db)
        if limit_err:
            return limit_err

        try:
            result = refine_prompt(session_id, mode=mode, db_path=current_db)
            if user_id:
                record_llm_usage(user_id, request.path, success=1, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            return jsonify({"error": str(ve)}), 400
        except Exception:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            raise

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

        limit_err = check_and_enforce_daily_limit(user_id, db_path=current_db)
        if limit_err:
            return limit_err

        try:
            result = improve_prompt(prompt.strip(), user_id=user_id, db_path=current_db)
            if user_id:
                record_llm_usage(user_id, request.path, success=1, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            return jsonify({"error": str(ve)}), 400
        except Exception:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            raise

    @app.route("/api/sessions/<int:session_id>/score", methods=["POST"])
    @limiter.limit("20 per minute")
    def score_prompt_route(session_id: int) -> tuple[Response, int]:
        """Score the latest final prompt of a session on clarity, specificity, and completeness."""
        current_db = app.config.get("DB_PATH")
        user_id = session.get("user_id")
        session_row = get_session(session_id, user_id=user_id, db_path=current_db)
        if session_row is None:
            return jsonify({"error": "Session not found"}), 404

        limit_err = check_and_enforce_daily_limit(user_id, db_path=current_db)
        if limit_err:
            return limit_err

        try:
            result = score_prompt(session_id, db_path=current_db)
            if user_id:
                record_llm_usage(user_id, request.path, success=1, db_path=current_db)
            return jsonify(result), 200
        except ValueError as ve:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            return jsonify({"error": str(ve)}), 400
        except Exception:
            if user_id:
                record_llm_usage(user_id, request.path, success=0, db_path=current_db)
            raise

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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
