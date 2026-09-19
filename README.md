# Prompter

A small web app that turns a rough idea into a structured, model-agnostic prompt you can paste into Claude, ChatGPT, Gemini, or any other AI.

You type an idea, the app asks a few short follow-up questions (max 4), then generates one clean prompt with sections like Role, Context, Task, Constraints, and Output format.

## Features

- Chat-style flow: asks only for what's missing, or skips straight to the prompt if your idea is already detailed
- "Skip questions" button that builds the prompt right away, with `[PLACEHOLDER]`s where info is missing
- Idea type detection (study, writing, research, other) with matching prompt templates
- Saved history in SQLite, with type filters and search
- One-click copy of the final prompt
- "Shorter" and "More detailed" variants, with a version switcher
- "Improve my prompt" mode for existing prompts
- Prompt scoring (clarity, specificity, completeness)

## Tech stack

- Python 3.11+ and Flask
- Groq API (`groq` SDK)
- SQLite (stdlib `sqlite3`)
- Vanilla HTML, CSS, and JavaScript

## Setup

1. Clone the repo and open the folder.

2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root (see `.env.example`):

   ```
   GROQ_API_KEY=your_groq_api_key
   GROQ_MODEL=openai/gpt-oss-120b
   ```

   Get a key from the [Groq Console](https://console.groq.com). Model availability depends on your account, so if you get a 404 `model_not_found` error, list the models your key can access and set `GROQ_MODEL` to one of them.

4. Run the app:

   ```bash
   flask --app app run
   ```

   Then open the local URL Flask prints (usually `http://127.0.0.1:5000`).

## Project structure

```
├── app.py               # Flask app and routes
├── db.py                # SQLite helpers and schema
├── services/
│   ├── llm.py           # the only place that calls Groq
│   └── builder.py       # chat-loop logic
├── prompts/             # system prompts as text files
│   ├── brain.txt
│   └── types/
├── templates/index.html
├── static/              # app.js, style.css
├── tests/
├── SPEC.md              # full build spec
└── AGENTS.md            # rules for the coding agent
```

## API

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/sessions` | Start a session with an idea |
| POST | `/api/sessions/<id>/messages` | Answer a question (or `skip: true` to generate now) |
| GET | `/api/sessions` | List history |
| GET | `/api/sessions/<id>` | Load one session |
| DELETE | `/api/sessions/<id>` | Delete a session |

All errors return `{"error": "message"}`.

## Tests

```bash
pytest
```

The LLM is mocked in tests, so no API calls are made.

## Notes

- Keep `.env` and `data/` out of git. Both are in `.gitignore`.
- This app has no authentication. It is meant for local, personal use. Add a login and rate limiting before hosting it publicly, otherwise anyone with the URL can spend your Groq quota.

## Roadmap

- Password login and rate limiting for hosting
- More type templates (coding, image generation, business)
- Streaming responses
- Export history as JSON or markdown
