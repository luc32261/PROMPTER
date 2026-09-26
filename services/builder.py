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


def build_system_prompt(
    session_type: str | None,
    attachment_text: str | None = None,
    attachment_filename: str | None = None,
    attachment_summarized: bool = False,
) -> str:
    """Compose the system prompt from brain.txt, type hints, and attachment context."""
    base = (PROMPTS / "brain.txt").read_text(encoding="utf-8")
    if session_type in {"study", "writing", "research", "other"}:
        type_file = PROMPTS / "types" / f"{session_type}.txt"
        if type_file.exists():
            base += "\n\n" + type_file.read_text(encoding="utf-8")
    if attachment_text and attachment_text.strip():
        filename = attachment_filename or "document"
        if attachment_summarized:
            att_file = PROMPTS / "attachment_context_long.txt"
        else:
            att_file = PROMPTS / "attachment_context_short.txt"
        if not att_file.exists():
            att_file = PROMPTS / "attachment_context.txt"
        if att_file.exists():
            att_template = att_file.read_text(encoding="utf-8")
            att_content = att_template.replace("{material}", attachment_text.strip()).replace("{filename}", filename)
            base += "\n\n" + att_content
    return base


def parse_reply(raw: str, force_ready: bool) -> dict[str, Any] | None:
    """Parse and validate the JSON reply from the model against the contract."""
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Extract JSON object if surrounded by markdown or commentary
    match = re.search(r"(\{[\s\S]*\})", text)
    extracted = match.group(1) if match else text

    data = None
    try:
        data = json.loads(extracted)
    except (json.JSONDecodeError, TypeError):
        # Attempt recovery on common trailing LLM JSON glitches
        cleaned = re.sub(r'",\s*"*\}\}\s*$', '"}', extracted)
        cleaned = re.sub(r',\s*\}', '}', cleaned)
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return None

    if not isinstance(data, dict):
        return None

    # Handle alternate prompt keys (e.g. "prompt" instead of "final_prompt")
    if "prompt" in data and "final_prompt" not in data:
        data["final_prompt"] = data["prompt"]

    status = data.get("status")
    if (status == "ready" or (status is None and "final_prompt" in data)) and isinstance(data.get("final_prompt"), str) and data.get("final_prompt", "").strip():
        data["status"] = "ready"
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
    attachment_text = session["attachment_text"] if "attachment_text" in session.keys() else None
    attachment_filename = session["attachment_filename"] if "attachment_filename" in session.keys() else None
    attachment_summarized = bool(session["attachment_summarized"]) if "attachment_summarized" in session.keys() else False

    system_prompt = build_system_prompt(
        session["type"],
        attachment_text=attachment_text,
        attachment_filename=attachment_filename,
        attachment_summarized=attachment_summarized,
    )

    prior_asks = count_prior_questions(history)
    force_ready = (prior_asks >= MAX_QUESTIONS) or skip

    messages_for_llm: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for msg in history:
        messages_for_llm.append({"role": msg["role"], "content": msg["content"]})

    if attachment_text:
        attachment_directive = (
            f"REMINDER FOR ATTACHED MATERIAL ('{attachment_filename or 'document'}'): "
            "You MUST ground all responses and the final prompt in the actual subject matter, concepts, and terms from the attached material. "
            "NEVER generate generic descriptions like 'the provided document' or 'read the document'; name the specific topic and concepts directly."
        )
        if force_ready:
            ready_instruction = f"Generate the final prompt now. status MUST be 'ready'. {attachment_directive}"
            if attachment_summarized:
                ready_instruction += f" Include [PASTE FULL {attachment_filename or 'document'} HERE]."
            else:
                ready_instruction += " Embed the source material inline under '## Source Material'."
            messages_for_llm.append({"role": "system", "content": ready_instruction})
        else:
            messages_for_llm.append({"role": "system", "content": attachment_directive})
    elif force_ready:
        messages_for_llm.append({"role": "system", "content": "Generate the final prompt now. status MUST be 'ready'."})

    step_name = "final_generation" if force_ready else "follow_up_question"
    print(
        f"\n[builder:run_turn:pre_call_llm] session_id={session_id} step={step_name} "
        f"messages_count={len(messages_for_llm)} attachment_file={attachment_filename!r} "
        f"summarized={attachment_summarized} attachment_len={len(attachment_text) if attachment_text else 0}",
        flush=True,
    )
    for idx, msg in enumerate(messages_for_llm):
        has_att = bool(attachment_text and attachment_text in msg["content"])
        snippet = (msg["content"][:160] + "...") if len(msg["content"]) > 160 else msg["content"]
        print(
            f"  msg[{idx}] role={msg['role']} len={len(msg['content'])} "
            f"has_attachment_text={has_att} snippet={snippet!r}",
            flush=True,
        )

    raw = call_llm(messages_for_llm, json_mode=True, max_tokens=4000)
    parsed = parse_reply(raw, force_ready=force_ready)

    if parsed is None:
        finish_reason = get_last_finish_reason()
        print(f"[parse_reply failed] finish_reason={finish_reason}\nraw={raw}", flush=True)
        retry_instruction = (
            "Your last reply was invalid. Generate the final prompt now. status MUST be 'ready' with 'final_prompt'."
            if force_ready
            else "Your last reply was invalid. Return only valid JSON in the required shape."
        )
        retry_messages = list(messages_for_llm) + [
            {"role": "assistant", "content": raw},
            {"role": "system", "content": retry_instruction},
        ]
        retry_raw = call_llm(retry_messages, json_mode=True, max_tokens=4000)
        parsed = parse_reply(retry_raw, force_ready=force_ready)
        raw_to_save = retry_raw

        if parsed is None and force_ready:
            candidate = retry_raw or raw
            is_json_ask = False
            try:
                candidate_data = json.loads(candidate)
                if isinstance(candidate_data, dict) and (candidate_data.get("status") == "ask" or "question" in candidate_data):
                    is_json_ask = True
            except Exception:
                pass

            if candidate and not is_json_ask and ("## Role" in candidate or "## Task" in candidate):
                parsed = {
                    "status": "ready",
                    "type": session["type"] if session and session["type"] else "other",
                    "final_prompt": candidate.strip(),
                }
                raw_to_save = json.dumps(parsed)
            else:
                force_messages = list(messages_for_llm) + [
                    {
                        "role": "user",
                        "content": (
                            "You must stop asking questions now. Based on all our conversation and the provided material above, "
                            "generate the final prompt immediately in the required JSON shape: "
                            '{"status": "ready", "type": "' + (session["type"] or "other") + '", "final_prompt": "## Role\\n...\\n## Task\\n..."}'
                        ),
                    }
                ]
                force_raw = call_llm(force_messages, json_mode=True, max_tokens=4000)
                parsed = parse_reply(force_raw, force_ready=True)
                if parsed is not None:
                    raw_to_save = force_raw
                else:
                    fallback_ask = parse_reply(candidate, force_ready=False)
                    if fallback_ask:
                        parsed = fallback_ask
                        raw_to_save = candidate

        if parsed is None:
            finish_reason = get_last_finish_reason()
            print(f"[parse_reply retry failed] finish_reason={finish_reason}\nretry_raw={retry_raw}", flush=True)
            raise ModelOutputError("Model returned invalid output")
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
    result["has_attachment"] = bool(session["has_attachment"]) if "has_attachment" in session.keys() else False
    result["attachment_filename"] = session["attachment_filename"] if "attachment_filename" in session.keys() else None
    result["attachment_summarized"] = bool(session["attachment_summarized"]) if "attachment_summarized" in session.keys() else False
    return result


def create_session_with_idea(
    idea: str,
    user_id: int | None = None,
    attachment: tuple[str, bytes] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new session, record the idea as user message, and run the first turn."""
    has_attachment = 0
    attachment_filename = None
    attachment_text = None
    attachment_summarized = 0

    if attachment is not None:
        filename, file_bytes = attachment
        try:
            from services.extractor import process_attachment
            processed_text, was_summarized = process_attachment(file_bytes, filename)
            if processed_text and processed_text.strip():
                has_attachment = 1
                attachment_filename = filename
                attachment_text = processed_text.strip()
                attachment_summarized = 1 if was_summarized else 0
        except Exception as exc:
            # Graceful degradation: flow continues with user's idea alone
            print(f"[Attachment extraction failed for {filename}]: {exc}", flush=True)
            has_attachment = 0
            attachment_filename = None
            attachment_text = None
            attachment_summarized = 0

    session_id = create_session(
        idea,
        user_id=user_id,
        has_attachment=has_attachment,
        attachment_filename=attachment_filename,
        attachment_text=attachment_text,
        attachment_summarized=attachment_summarized,
        db_path=db_path,
    )
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


