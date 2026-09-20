# Prompt Builder (PROMPTER)

A local web application that turns rough ideas into structured, model-agnostic prompts ready to paste into Claude, ChatGPT, Gemini, etc. The app asks up to 4 quick clarifying questions or lets you skip straight to generation.

---

## Features

- **Conversational Builder**: Interactive chat loop with a maximum of 4 clarifying questions.
- **Model-Agnostic Prompts**: Generates clean Markdown prompts with standard sections (`## Role`, `## Context`, `## Task`, `## Constraints`, `## Output format`).
- **One-Click Refinements**: Generate "Shorter" or "More detailed" variants with a built-in version switcher.
- **Improve My Prompt Mode**: Paste an existing prompt to enhance its structure and view a "what I changed" breakdown.
- **Prompt Scoring**: Instant 1–10 rating across Clarity, Specificity, and Completeness with concrete suggestions.
- **History & Search**: Persistent SQLite storage with real-time text search and category filter chips (`Study`, `Writing`, `Research`, `Other`).
- **Multi-User Authentication**:
  - User registration (`/register`) and login (`/login`) with session cookies.
  - Secure password hashing using `werkzeug.security`.
  - User-isolated session history and prompts.
- **Sharing Readiness & Reliability**:
  - Rate limiting on LLM routes (20 requests/minute per IP) via `flask-limiter`.
  - Self-healing JSON parser with `failed_generation` recovery and auto-formatting.
  - Configurable database location via `DATABASE_PATH`.
  - Production-ready with `gunicorn`.

---

## Environment Variables

Copy `.env.example` to `.env` and set your configuration:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `GROQ_API_KEY` | **Required.** Your Groq API key (`gsk_...`). | *None* |
| `GROQ_MODEL` | The LLM model to use on Groq. | `llama-3.3-70b-versatile` (or `openai/gpt-oss-120b`) |
| `APP_PASSWORD` | Shared default password fallback. | `admin` |
| `SECRET_KEY` | Secret key used by Flask to sign session cookies. | `dev-secret-key-change-in-production` |
| `DATABASE_PATH` | Path to the SQLite database file. | `data/app.db` |
| `ADMIN_USERNAME` | Username for the single admin account. | `admin` |
| `ADMIN_PASSWORD` | Password for the single admin account. | *None* |
| `ADMIN_GATE_PASSWORD` | Password for the first-layer admin gate. | *None* |
| `DAILY_LIMIT` | Default daily limit of LLM calls per user (UTC day). | `100` |
| `FLASK_ENV` | Environment mode (`production` or `development`). Setting to `production` enables HTTPS secure cookies (`SESSION_COOKIE_SECURE=True`) and enforces mandatory admin variables. | `development` |

---

## Admin

The application includes an administrative dashboard and management system with multi-layer security:

- **Two-Layer Entry & Gate**:
  - A subtle "Admin" link on the login page leads to `/admin/gate`, protected by `ADMIN_GATE_PASSWORD`.
  - Passing the gate grants a 10-minute window to access `/admin/login`, requiring `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
  - Logging in successfully grants a 30-minute session to access `/admin`.
  - Non-admin users or unauthorized requests to `/admin` routes return HTTP 404 (`{"error": "Not Found"}`).
  - Both gate and login endpoints enforce rate limiting (max 5 failed attempts per 15 minutes).
- **User Management**:
  - Single admin model: only `ADMIN_USERNAME` can hold the admin role; public registration cannot create or elevate admins.
  - View all user accounts with creation date, last login, prompt count, calls today, daily limit, and status.
  - Per-user actions (CSRF-protected POST routes):
    - **Disable / Enable**: Toggles user active state (disabling invalidates active sessions).
    - **Force Logout**: Bumps user session version to invalidate all active logins.
    - **Reset Password**: Generates a random temporary password shown once to the admin (only the hash is stored).
    - **Delete User**: Cascades deletion of the user and their sessions, messages, and prompts.
    - Admins cannot disable, force logout, or delete their own account.
- **Usage & Daily Quotas**:
  - Daily LLM call tracking partitioned by UTC day (`00:00 UTC` reset).
  - Configurable global fallback (`DAILY_LIMIT`, default 100) and custom per-user limits editable from `/admin`.
  - The admin account is exempt from daily limits.
- **Audit Logging**:
  - Tracks the last 50 administrative and security events: gate successes/failures, admin login successes/failures, status changes, force logouts, password resets, limit changes, and deletions.
  - Stores usernames instead of foreign keys so audit history survives user deletion.
  - Passwords are never stored in the audit log.

---

## Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/luc32261/PROMPTER.git
   cd PROMPTER
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .\.venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure your `.env` file:**
   ```bash
   GROQ_API_KEY=your_groq_api_key_here
   SECRET_KEY=your_flask_secret_key
   ```

---

## Running the Application

### Development (Flask)
```bash
python app.py
```
Visit `http://localhost:5000` to create an account or sign in.

### Production (Gunicorn)
```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

---

## Running Tests

Run the full automated test suite using `pytest`:

```bash
pytest -v
```
All unit and API tests use mocked LLM responses with zero API calls.
