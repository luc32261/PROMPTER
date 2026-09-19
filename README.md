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
