# Agent rules

- Stack is fixed: Python 3.11+, Flask, `groq` SDK, SQLite via stdlib `sqlite3`, vanilla HTML/CSS/JS. No React, no ORM, no extra frameworks unless asked.
- Never hardcode secrets. Read `GROQ_API_KEY` and `GROQ_MODEL` from `.env` (python-dotenv). Commit `.env.example`, never `.env`.
- All LLM calls go through `services/llm.py::call_llm`. Nothing else imports `groq`.
- All prompts live as text files in `prompts/`, never as string literals in Python.
- Keep functions small, add type hints and short docstrings. No dead code.
- The API always returns JSON with a consistent error shape: `{"error": "message"}`.
- After each phase: run the app, run the tests for that phase, and summarize what changed. Do not start the next phase on your own.
- Full details are in SPEC.md. Read it before starting any phase.
