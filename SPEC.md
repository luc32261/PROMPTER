# Prompt Builder: Antigravity Build Spec

A local web app. The user types a rough idea, the app asks a few short follow-up questions, then outputs one structured, model-agnostic prompt they can paste into Claude, ChatGPT, Gemini, etc.

Use this file two ways:
1. Drop the whole thing in the repo root as `SPEC.md` (and copy section 1 into `AGENTS.md`) so the agent always has context.
2. Paste the phase prompts from section 9 into Antigravity **one at a time**. Review and run each phase before starting the next.

---

## 1. Agent rules (copy into AGENTS.md)

- Stack is fixed: Python 3.11+, Flask, `groq` SDK, SQLite via stdlib `sqlite3`, vanilla HTML/CSS/JS. No React, no ORM, no extra frameworks unless asked.
- Never hardcode secrets. Read `GROQ_API_KEY` and `GROQ_MODEL` from `.env` (python-dotenv). Commit `.env.example`, never `.env`.
- All LLM calls go through `services/llm.py::call_llm`. Nothing else imports `groq`.
- All prompts live as text files in `prompts/`, never as string literals in Python.
- Keep functions small, add type hints and short docstrings. No dead code.
- The API always returns JSON with a consistent error shape: `{"error": "message"}`.
- After each phase: run the app, run the tests for that phase, and summarize what changed. Do not start the next phase on your own.

---

## 2. Tech stack

| Part | Choice |
|---|---|
| Backend | Flask |
| LLM | Groq API, model set by `GROQ_MODEL` (default `llama-3.3-70b-versatile`; check Groq's model list if it errors) |
| DB | SQLite file `data/app.db` |
| Frontend | Single page: `templates/index.html` + `static/app.js` + `static/style.css` |
| Config | `.env` via python-dotenv |

`requirements.txt`: `flask`, `groq`, `python-dotenv`, `pytest`

---

## 3. Folder structure

```
prompt-builder/
├── AGENTS.md
├── SPEC.md
├── .env.example
├── .gitignore
├── requirements.txt
├── app.py                  # Flask app factory + routes
├── db.py                   # sqlite helpers + schema init
├── services/
│   ├── llm.py              # call_llm() -> Groq
│   └── builder.py          # the chat-loop logic
├── prompts/
│   ├── brain.txt           # base system prompt
│   └── types/
│       ├── study.txt
│       ├── writing.txt
│       ├── research.txt
│       └── other.txt
├── templates/index.html
├── static/app.js
├── static/style.css
├── data/                   # app.db lives here (gitignored)
└── tests/
    ├── test_builder.py
    └── test_api.py
```

`.env.example`:
```
GROQ_API_KEY=your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

---

## 4. Database schema

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  idea        TEXT NOT NULL,
  type        TEXT,                       -- study | writing | research | other
  status      TEXT NOT NULL DEFAULT 'asking',  -- asking | ready
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role        TEXT NOT NULL,              -- user | assistant
  content     TEXT NOT NULL,              -- assistant content stored as raw JSON string
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  final_prompt TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Enable `PRAGMA foreign_keys = ON` on every connection.

---

## 5. LLM contract

The model must return **only JSON**, in one of two shapes:

```json
{"status": "ask", "type": "study", "question": "What level is this for, school or college?"}
{"status": "ready", "type": "study", "final_prompt": "## Role\n..."}
```

`services/llm.py` reference:

```python
import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
_client = Groq(api_key=os.environ["GROQ_API_KEY"])
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

def call_llm(messages: list[dict], json_mode: bool = True, temperature: float = 0.4) -> str:
    """Single entry point for all LLM calls."""
    kwargs = {"model": MODEL, "messages": messages, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content
```

---

## 6. The brain: `prompts/brain.txt`

```
You are a prompt engineer. The user gives you a rough idea. Turn it into ONE
high-quality prompt they can paste into any AI (Claude, ChatGPT, Gemini).

PROCESS
1. Classify the idea as one of: study, writing, research, other.
2. Check what is missing from: goal, audience/level, format, length/depth,
   constraints, tone. If something important is missing, ask about ONLY the
   single most important missing piece. One short question at a time.
   If the idea is already clear enough, ask nothing and generate.
3. When you have enough info, or you are told to generate now, produce the
   final prompt.

FINAL PROMPT RULES
- Sections, in order, dropping any that add nothing:
  Role, Context, Task, Constraints, Output format, and optionally Examples/Tone.
- Model-agnostic: plain markdown headers (## Role), no model-specific tricks,
  no XML tags, no mention of any specific AI product.
- Be specific and concrete. Never invent facts about the user.
- Where info is missing, use a sensible default or a [PLACEHOLDER] the user can fill.
- The prompt must be fully self-contained. Never reference this conversation.
- Keep it as short as it can be while staying complete. No filler.

OUTPUT
Respond with ONLY a JSON object, nothing else, in one of these shapes:
{"status":"ask","type":"<type>","question":"<one short question>"}
{"status":"ready","type":"<type>","final_prompt":"<the prompt, markdown, \n for newlines>"}
```

Type hint files get appended to the system prompt once the session's type is known.

`prompts/types/study.txt`
```
TYPE HINTS (study)
Pin down: subject and topic, level (school/college/exam prep), goal (understand vs
memorize vs exam answers), and structure wanted (notes, Q&A, step-by-step, flashcards,
summary). Ask for examples and exam-style output when relevant.
```

`prompts/types/writing.txt`
```
TYPE HINTS (writing)
Pin down: what is being written, audience, tone/voice, length, format, and what to
avoid. If the user has a sample or draft, the prompt should tell the AI to match it.
```

`prompts/types/research.txt`
```
TYPE HINTS (research)
Pin down: scope, depth, time frame, what kind of sources are expected, and the output
structure (overview, comparison table, pros/cons, citations). The prompt should tell
the AI to flag uncertainty and not invent sources.
```

`prompts/types/other.txt`
```
TYPE HINTS (other)
Use the general rules only. Infer the best structure from the idea itself.
```

---

## 7. Builder logic: `services/builder.py`

Behavior:

1. `MAX_QUESTIONS = 4` (enforced in code, not just in the prompt).
2. Build messages: system prompt = `brain.txt` + type hint (if session type known), then the full message history for the session (user text as-is, assistant turns as their stored raw JSON).
3. Count prior assistant turns with `status == "ask"`. If count >= `MAX_QUESTIONS` **or** the request has `skip=true`, append a final system message: `"Generate the final prompt now. status MUST be 'ready'."`
4. Call `call_llm`. Parse JSON. Validate:
   - `status` in `{"ask", "ready"}`
   - `ask` has non-empty `question`; `ready` has non-empty `final_prompt`
   - if forced-generate and status is `ask`, treat as invalid
5. On invalid output, retry **once** with an extra system message: `"Your last reply was invalid. Return only valid JSON in the required shape."` If still invalid, raise an error, which the API returns as 502 `{"error": "Model returned invalid output"}`.
6. Persist: save the assistant message (raw JSON string). Update `sessions.type` from the response. If `ready`, set `sessions.status = 'ready'` and insert into `prompts`.
7. Return the parsed dict plus `session_id`.

Reference skeleton:

```python
import json
from pathlib import Path
from services.llm import call_llm

MAX_QUESTIONS = 4
PROMPTS = Path(__file__).parent.parent / "prompts"

def build_system_prompt(session_type: str | None) -> str:
    base = (PROMPTS / "brain.txt").read_text()
    if session_type in {"study", "writing", "research", "other"}:
        base += "\n\n" + (PROMPTS / "types" / f"{session_type}.txt").read_text()
    return base

def parse_reply(raw: str, force_ready: bool) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    status = data.get("status")
    if status == "ready" and data.get("final_prompt", "").strip():
        return data
    if status == "ask" and not force_ready and data.get("question", "").strip():
        return data
    return None
```

---

## 8. API and UI

### API (all JSON)

| Method | Route | Body | Returns |
|---|---|---|---|
| POST | `/api/sessions` | `{"idea": "..."}` | `{session_id, status, type, question?, final_prompt?}` |
| POST | `/api/sessions/<id>/messages` | `{"text": "...", "skip": false}` | same shape as above |
| GET | `/api/sessions` | | list of `{id, idea, type, status, created_at}` newest first |
| GET | `/api/sessions/<id>` | | session + messages + final_prompt if any |
| DELETE | `/api/sessions/<id>` | | `{ok: true}` |

Validation: empty or missing text returns 400. Unknown session returns 404. Keep input under 4000 chars.

### UI (single page)

- Left sidebar: "New prompt" button, history list (idea snippet, type tag, date), delete icon per item. Clicking loads that session.
- Main pane: chat bubbles (user right, app left). Input box at the bottom, Enter to send, Shift+Enter for newline.
- "Skip questions, just build it" button, visible while status is `asking`.
- When ready: show the final prompt in a distinct card (monospace, scrollable) with a **Copy** button that shows "Copied!" for 1.5s.
- Loading state while waiting on the model. Friendly error message on failure with a retry.
- Dark theme, clean, responsive down to phone width. No external CDN dependencies.

---

## 9. Phase prompts (paste one at a time)

### Phase 0: Scaffold
```
Read SPEC.md and AGENTS.md. Scaffold the project exactly per section 3 (folders, empty
files where needed), create requirements.txt, .env.example, .gitignore (ignore .env,
data/, __pycache__, .venv), and a minimal Flask app in app.py with a health route
GET /api/health returning {"ok": true}. Create a venv, install requirements, run the
app, and confirm the health route works. Do not implement anything else.
```
**Done when:** `GET /api/health` returns `{"ok": true}`.

### Phase 1: LLM + brain
```
Implement services/llm.py exactly as in SPEC.md section 5. Create prompts/brain.txt
and the four files in prompts/types/ with the exact content from SPEC.md section 6.
Write a small script scripts/try_brain.py that takes an idea from the command line,
sends it to call_llm with the brain prompt as the system message, and prints the raw
JSON. Run it with 3 sample ideas (one study, one writing, one research) and show me
the outputs. Do not touch the Flask routes yet.
```
**Done when:** all three ideas return valid JSON in one of the two shapes.

### Phase 2: Builder logic
```
Implement db.py (schema from SPEC.md section 4, foreign keys on) and
services/builder.py per SPEC.md section 7: MAX_QUESTIONS enforcement, skip handling,
JSON validation, one retry, persistence of messages/type/status/prompts. Write
tests/test_builder.py using a mocked call_llm (no real API calls) covering: normal ask
then ready flow, forced generation after 4 questions, skip=true, invalid JSON then a
successful retry, and invalid twice raising an error. Run pytest and show the results.
```
**Done when:** all tests pass with the LLM mocked.

### Phase 3: API routes
```
Implement all routes in SPEC.md section 8 in app.py using services/builder.py and
db.py. Enforce the validation rules and the consistent {"error": "..."} shape.
Write tests/test_api.py with the LLM mocked, covering each route including 400 and 404
cases. Also do a real end-to-end run with curl against the live Groq API using one
study idea and show me the full conversation.
```
**Done when:** curl flow goes idea → question(s) → final prompt, and `GET /api/sessions` shows it.

### Phase 4: Frontend
```
Build the single-page UI per SPEC.md section 8 (templates/index.html, static/app.js,
static/style.css). Vanilla JS only, no CDNs. Include sidebar history, chat pane,
skip button, final-prompt card with Copy button, loading and error states, dark theme,
responsive layout. Render the final prompt as plain text in a <pre>, never as HTML
(avoid XSS). Run the app and test the full flow in the browser, then give me a
screenshot or a description of each state (empty, asking, ready, error).
```
**Done when:** you can go from idea to copied prompt entirely in the browser.

### Phase 5: Polish and variants
```
Add three things:
1. "Shorter" and "More detailed" buttons on the final prompt card. Add route
   POST /api/sessions/<id>/refine with body {"mode": "shorter" | "detailed"}. It sends
   the current final prompt plus a short instruction to call_llm and saves the result
   as a new row in prompts. The card shows the newest version with a small version
   switcher.
2. Type filter chips above the history list (all / study / writing / research / other)
   and a text search box that filters by idea.
3. Keyboard shortcut Ctrl+K to focus the input, and Ctrl+Enter to send.
Add tests for the refine route with the LLM mocked. Do not change existing behavior.
```

### Phase 6: Extras (optional)
```
Add an "Improve my prompt" mode: a toggle next to New prompt. In this mode the user
pastes an existing prompt and the app returns an improved version using the same final
prompt rules from prompts/brain.txt, with a short "what I changed" list. Put the mode's
system prompt in prompts/improve.txt. Then add a "Score" button on the final prompt card
that makes a second LLM call rating it 1-10 on clarity, specificity, and completeness
with two concrete suggestions. Keep both behind separate routes and add mocked tests.
```

---

## 10. Manual test checklist

- [ ] Vague idea ("help me study") triggers 1-4 sensible questions, then a prompt
- [ ] Detailed idea ("explain Kirchhoff's laws for 1st year engineering, exam-style answers, with 2 numericals") skips questions and generates immediately
- [ ] "Skip questions" produces a prompt with sensible `[PLACEHOLDER]`s
- [ ] After 4 questions the app forces a final prompt
- [ ] Refresh the page: history persists, clicking an old session restores it
- [ ] Delete removes the session and its messages/prompts
- [ ] Wrong or missing API key shows a clear error, not a crash
- [ ] Pasting `<script>alert(1)</script>` as an idea does not execute anything
- [ ] The generated prompt works when pasted into Claude, ChatGPT, and Gemini

---

## 11. Later roadmap

- Add more type templates (coding, image generation, business) by dropping a new file in `prompts/types/` and adding the type to the allowed set
- Streaming responses for the chat
- Export history as JSON/markdown
- Optional auth once it goes beyond personal use (Flask-Login or hosted auth), plus per-user history
- Swap providers: only `services/llm.py` changes
