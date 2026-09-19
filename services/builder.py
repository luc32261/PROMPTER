import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from db import (
    add_message,
    create_session,
    get_messages,
    get_session,
    save_final_prompt,
    update_session,
)
from services.llm import call_llm, get_last_finish_reason

MAX_QUESTIONS = 4
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class ModelOutputError(Exception):
    """Raised when the LLM returns invalid JSON or schema after retry."""
    pass


def build_system_prompt(session_type: str | None) -> str:
    """Compose the system prompt from brain.txt and any applicable type hints."""
    base = (PROMPTS / "brain.txt").read_text(encoding="utf-8")
    if session_type in {"study", "writing", "research", "other"}:
        type_file = PROMPTS / "types" / f"{session_type}.txt"
        if type_file.exists():
            base += "\n\n" + type_file.read_text(encoding="utf-8")
    return base


def parse_reply(raw: str, force_ready: bool) -> dict[str, Any] | None:
    """Parse and validate the JSON reply from the model against the contract."""
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    data = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Attempt recovery on common trailing LLM JSON glitches
        cleaned = re.sub(r'",\s*"*\}\}\s*$', '"}', text)
        cleaned = re.sub(r',\s*\}', '}', cleaned)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None

    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if status == "ready" and isinstance(data.get("final_prompt"), str) and data.get("final_prompt", "").strip():
        return data
    if status == "ask" and not force_ready and isinstance(data.get("question"), str) and data.get("question", "").strip():
        return data
    return None


def count_prior_questions(messages: list[sqlite3.Row | dict[str, Any]]) -> int:
    """Count prior assistant turns with status == 'ask'."""
    count = 0
    for msg in messages:
        if msg["role"] == "assistant":
            try:
                data = json.loads(msg["content"])
                if isinstance(data, dict) and data.get("status") == "ask":
                    count += 1
            except (json.JSONDecodeError, TypeError):
                continue
    return count


def run_turn(
    session_id: int,
    skip: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute a single builder turn for a session and persist results."""
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise KeyError(f"Session {session_id} not found")

    history = get_messages(session_id, db_path=db_path)
    system_prompt = build_system_prompt(session["type"])

    prior_asks = count_prior_questions(history)
    force_ready = (prior_asks >= MAX_QUESTIONS) or skip

    messages_for_llm: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for msg in history:
        messages_for_llm.append({"role": msg["role"], "content": msg["content"]})

    if force_ready:
        messages_for_llm.append(
            {"role": "system", "content": "Generate the final prompt now. status MUST be 'ready'."}
        )

    raw = call_llm(messages_for_llm, json_mode=True, max_tokens=4000)
    parsed = parse_reply(raw, force_ready=force_ready)

    if parsed is None:
        finish_reason = get_last_finish_reason()
        print(f"[parse_reply failed] finish_reason={finish_reason}\nraw={raw}", flush=True)
        retry_messages = list(messages_for_llm) + [
            {"role": "assistant", "content": raw},
            {
                "role": "system",
                "content": "Your last reply was invalid. Return only valid JSON in the required shape.",
            },
        ]
        retry_raw = call_llm(retry_messages, json_mode=True, max_tokens=4000)
        parsed = parse_reply(retry_raw, force_ready=force_ready)
        if parsed is None:
            finish_reason = get_last_finish_reason()
            print(f"[parse_reply retry failed] finish_reason={finish_reason}\nretry_raw={retry_raw}", flush=True)
            raise ModelOutputError("Model returned invalid output")
        raw_to_save = retry_raw
    else:
        raw_to_save = raw

    # Persist assistant message
    add_message(session_id, "assistant", raw_to_save, db_path=db_path)

    # Update session type if returned
    resp_type = parsed.get("type")
    if resp_type:
        update_session(session_id, session_type=resp_type, db_path=db_path)

    # If ready, update status and store prompt
    if parsed.get("status") == "ready":
        update_session(session_id, status="ready", db_path=db_path)
        save_final_prompt(session_id, parsed["final_prompt"], db_path=db_path)

    result = dict(parsed)
    result["session_id"] = session_id
    return result


def create_session_with_idea(
    idea: str,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new session, record the idea as user message, and run the first turn."""
    session_id = create_session(idea, user_id=user_id, db_path=db_path)
    add_message(session_id, "user", idea, db_path=db_path)
    return run_turn(session_id, skip=False, db_path=db_path)


def advance_session(
    session_id: int,
    text: str = "",
    skip: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Append user text (if provided) and run the next turn for a session."""
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise KeyError(f"Session {session_id} not found")

    if text.strip():
        add_message(session_id, "user", text.strip(), db_path=db_path)

    return run_turn(session_id, skip=skip, db_path=db_path)


def refine_prompt(
    session_id: int,
    mode: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refine an existing final prompt to be shorter or more detailed."""
    from db import get_all_prompts, get_final_prompt

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise KeyError(f"Session {session_id} not found")

    prompt_row = get_final_prompt(session_id, db_path=db_path)
    if prompt_row is None:
        raise ValueError("Session has no prompt to refine")

    if mode not in {"shorter", "detailed"}:
        raise ValueError("Mode must be 'shorter' or 'detailed'")

    instruction_file = PROMPTS / f"refine_{mode}.txt"
    instruction = instruction_file.read_text(encoding="utf-8")

    current_prompt = prompt_row["final_prompt"]
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": current_prompt},
    ]

    refined = call_llm(messages, json_mode=False)
    refined_clean = refined.strip()
    if not refined_clean:
        raise ModelOutputError("Model returned invalid output")

    save_final_prompt(session_id, refined_clean, db_path=db_path)
    all_prompts = get_all_prompts(session_id, db_path=db_path)
    prompt_list = [p["final_prompt"] for p in all_prompts]

    return {
        "session_id": session_id,
        "final_prompt": refined_clean,
        "prompts": prompt_list,
        "version": len(prompt_list),
    }


def improve_prompt(
    raw_prompt: str,
    user_id: int | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Improve an existing prompt directly and save as a ready session."""
    cleaned = raw_prompt.strip()
    if not cleaned:
        raise ValueError("Prompt must be a non-empty string")

    instruction = (PROMPTS / "improve.txt").read_text(encoding="utf-8")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": cleaned},
    ]

    raw = call_llm(messages, json_mode=True)
    parsed = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("final_prompt") and isinstance(data.get("changes"), list):
            parsed = data
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if parsed is None:
        retry_messages = list(messages) + [
            {"role": "assistant", "content": raw},
            {"role": "system", "content": "Your last reply was invalid. Return only valid JSON in the required shape."},
        ]
        retry_raw = call_llm(retry_messages, json_mode=True)
        try:
            data = json.loads(retry_raw)
            if isinstance(data, dict) and data.get("final_prompt") and isinstance(data.get("changes"), list):
                parsed = data
        except (json.JSONDecodeError, TypeError):
            parsed = None

        if parsed is None:
            raise ModelOutputError("Model returned invalid output")

    # Persist as a ready session
    idea_snippet = cleaned[:80] + ("..." if len(cleaned) > 80 else "")
    session_id = create_session(idea_snippet, user_id=user_id, db_path=db_path)
    update_session(session_id, status="ready", session_type="other", db_path=db_path)
    add_message(session_id, "user", cleaned, db_path=db_path)
    add_message(session_id, "assistant", json.dumps(parsed), db_path=db_path)
    save_final_prompt(session_id, parsed["final_prompt"], db_path=db_path)

    return {
        "session_id": session_id,
        "status": "ready",
        "type": "other",
        "final_prompt": parsed["final_prompt"],
        "changes": parsed["changes"],
        "prompts": [parsed["final_prompt"]],
    }


def score_prompt(
    session_id: int,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Score the latest final prompt for a session on clarity, specificity, and completeness."""
    from db import get_final_prompt

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise KeyError(f"Session {session_id} not found")

    prompt_row = get_final_prompt(session_id, db_path=db_path)
    if prompt_row is None:
        raise ValueError("Session has no prompt to score")

    instruction = (PROMPTS / "score.txt").read_text(encoding="utf-8")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": prompt_row["final_prompt"]},
    ]

    raw = call_llm(messages, json_mode=True)
    parsed = None
    try:
        data = json.loads(raw)
        if (
            isinstance(data, dict)
            and "clarity" in data
            and "specificity" in data
            and "completeness" in data
            and "suggestions" in data
        ):
            parsed = data
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if parsed is None:
        retry_messages = list(messages) + [
            {"role": "assistant", "content": raw},
            {"role": "system", "content": "Your last reply was invalid. Return only valid JSON in the required shape."},
        ]
        retry_raw = call_llm(retry_messages, json_mode=True)
        try:
            data = json.loads(retry_raw)
            if (
                isinstance(data, dict)
                and "clarity" in data
                and "specificity" in data
                and "completeness" in data
                and "suggestions" in data
            ):
                parsed = data
        except (json.JSONDecodeError, TypeError):
            parsed = None

        if parsed is None:
            raise ModelOutputError("Model returned invalid output")

    parsed["session_id"] = session_id
    return parsed


