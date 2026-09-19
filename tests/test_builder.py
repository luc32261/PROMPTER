import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from db import (
    get_final_prompt,
    get_messages,
    get_session,
    init_db,
)
from services.builder import (
    MAX_QUESTIONS,
    ModelOutputError,
    advance_session,
    build_system_prompt,
    count_prior_questions,
    create_session_with_idea,
    parse_reply,
)


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """Fixture to create and initialize a fresh SQLite database for each test."""
    db_file = tmp_path / "test.db"
    init_db(db_file)
    return db_file


def test_build_system_prompt() -> None:
    """Verify system prompt includes brain.txt and type hints when provided."""
    base_prompt = build_system_prompt(None)
    assert "You are a prompt engineer." in base_prompt

    study_prompt = build_system_prompt("study")
    assert "TYPE HINTS (study)" in study_prompt

    writing_prompt = build_system_prompt("writing")
    assert "TYPE HINTS (writing)" in writing_prompt


def test_parse_reply_validation() -> None:
    """Verify parse_reply validates status and required fields correctly."""
    # Invalid JSON
    assert parse_reply("not json", force_ready=False) is None

    # Valid ask
    ask_json = json.dumps({"status": "ask", "type": "study", "question": "What level?"})
    assert parse_reply(ask_json, force_ready=False) == {
        "status": "ask",
        "type": "study",
        "question": "What level?",
    }

    # Ask when force_ready is True should be invalid
    assert parse_reply(ask_json, force_ready=True) is None

    # Ask with empty question
    empty_q = json.dumps({"status": "ask", "type": "study", "question": "  "})
    assert parse_reply(empty_q, force_ready=False) is None

    # Valid ready
    ready_json = json.dumps({"status": "ready", "type": "study", "final_prompt": "## Role\nExpert"})
    assert parse_reply(ready_json, force_ready=True) == {
        "status": "ready",
        "type": "study",
        "final_prompt": "## Role\nExpert",
    }

    # Ready with empty final_prompt
    empty_p = json.dumps({"status": "ready", "type": "study", "final_prompt": "   "})
    assert parse_reply(empty_p, force_ready=True) is None


def test_normal_ask_then_ready_flow(test_db: Path) -> None:
    """Case 1: User gives idea -> app asks question -> user answers -> app produces ready prompt."""
    ask_reply = json.dumps({
        "status": "ask",
        "type": "study",
        "question": "What level of calculus is this for, school or college?",
    })
    ready_reply = json.dumps({
        "status": "ready",
        "type": "study",
        "final_prompt": "## Role\nCalculus Professor\n## Task\nExplain limits with step-by-step examples.",
    })

    with patch("services.builder.call_llm", return_value=ask_reply) as mock_llm:
        # Turn 1: Create session with idea
        res1 = create_session_with_idea("Help me study calculus limits", db_path=test_db)
        assert res1["status"] == "ask"
        assert res1["type"] == "study"
        assert res1["question"] == "What level of calculus is this for, school or college?"
        session_id = res1["session_id"]

        session = get_session(session_id, db_path=test_db)
        assert session is not None
        assert session["status"] == "asking"
        assert session["type"] == "study"

        messages = get_messages(session_id, db_path=test_db)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    with patch("services.builder.call_llm", return_value=ready_reply) as mock_llm:
        # Turn 2: User answers the question
        res2 = advance_session(session_id, text="College freshman level", skip=False, db_path=test_db)
        assert res2["status"] == "ready"
        assert res2["type"] == "study"
        assert "Calculus Professor" in res2["final_prompt"]

        session = get_session(session_id, db_path=test_db)
        assert session["status"] == "ready"

        prompt_row = get_final_prompt(session_id, db_path=test_db)
        assert prompt_row is not None
        assert "Calculus Professor" in prompt_row["final_prompt"]


def test_forced_generation_after_4_questions(test_db: Path) -> None:
    """Case 2: After 4 questions, builder appends forced generation system message."""
    ask_reply = json.dumps({
        "status": "ask",
        "type": "writing",
        "question": "Follow-up question?",
    })

    # Start session and accumulate 4 questions
    with patch("services.builder.call_llm", return_value=ask_reply):
        res = create_session_with_idea("Write an essay", db_path=test_db)
        session_id = res["session_id"]
        # Question 1 already recorded
        advance_session(session_id, text="Answer 1", db_path=test_db)  # Question 2
        advance_session(session_id, text="Answer 2", db_path=test_db)  # Question 3
        advance_session(session_id, text="Answer 3", db_path=test_db)  # Question 4

    history = get_messages(session_id, db_path=test_db)
    assert count_prior_questions(history) == 4

    ready_reply = json.dumps({
        "status": "ready",
        "type": "writing",
        "final_prompt": "## Role\nEssay writer\n## Task\nDraft full essay.",
    })

    with patch("services.builder.call_llm", return_value=ready_reply) as mock_llm:
        # 5th turn: prior_asks == 4 >= MAX_QUESTIONS -> force_ready must be true
        res = advance_session(session_id, text="Answer 4", db_path=test_db)
        assert res["status"] == "ready"

        # Verify the forced generation system message was sent in LLM call
        call_args, _ = mock_llm.call_args
        sent_messages = call_args[0]
        assert sent_messages[-1] == {
            "role": "system",
            "content": "Generate the final prompt now. status MUST be 'ready'.",
        }


def test_skip_true_forces_ready(test_db: Path) -> None:
    """Case 3: skip=True forces generation immediately even before 4 questions."""
    ask_reply = json.dumps({
        "status": "ask",
        "type": "research",
        "question": "What is the primary topic?",
    })
    ready_reply = json.dumps({
        "status": "ready",
        "type": "research",
        "final_prompt": "## Role\nResearcher\n## Task\nSynthesize state of art.",
    })

    with patch("services.builder.call_llm", return_value=ask_reply):
        res = create_session_with_idea("Research quantum computing", db_path=test_db)
        session_id = res["session_id"]

    with patch("services.builder.call_llm", return_value=ready_reply) as mock_llm:
        res2 = advance_session(session_id, skip=True, db_path=test_db)
        assert res2["status"] == "ready"
        assert res2["final_prompt"] == "## Role\nResearcher\n## Task\nSynthesize state of art."

        call_args, _ = mock_llm.call_args
        sent_messages = call_args[0]
        assert sent_messages[-1] == {
            "role": "system",
            "content": "Generate the final prompt now. status MUST be 'ready'.",
        }

        session = get_session(session_id, db_path=test_db)
        assert session["status"] == "ready"


def test_invalid_json_then_successful_retry(test_db: Path) -> None:
    """Case 4: Invalid JSON on first attempt triggers a retry with correction instruction."""
    invalid_first = "Sorry, here is the question: What is your audience?"
    valid_second = json.dumps({
        "status": "ask",
        "type": "writing",
        "question": "What is your target audience?",
    })

    with patch("services.builder.call_llm", side_effect=[invalid_first, valid_second]) as mock_llm:
        res = create_session_with_idea("Write a blog post", db_path=test_db)
        assert mock_llm.call_count == 2
        assert res["status"] == "ask"
        assert res["question"] == "What is your target audience?"

        # Verify retry message structure
        second_call_messages = mock_llm.call_args_list[1][0][0]
        assert second_call_messages[-2] == {"role": "assistant", "content": invalid_first}
        assert second_call_messages[-1] == {
            "role": "system",
            "content": "Your last reply was invalid. Return only valid JSON in the required shape.",
        }


def test_invalid_twice_raises_error(test_db: Path) -> None:
    """Case 5: Invalid JSON twice in a row raises ModelOutputError."""
    invalid_1 = "invalid json once"
    invalid_2 = json.dumps({"status": "non_existent_status"})

    with patch("services.builder.call_llm", side_effect=[invalid_1, invalid_2]) as mock_llm:
        with pytest.raises(ModelOutputError, match="Model returned invalid output"):
            create_session_with_idea("Some idea", db_path=test_db)
        assert mock_llm.call_count == 2


def test_database_cascade_delete(test_db: Path) -> None:
    """Verify that deleting a session cascades to messages and prompts."""
    from db import delete_session

    ready_reply = json.dumps({
        "status": "ready",
        "type": "other",
        "final_prompt": "## Role\nAssistant",
    })

    with patch("services.builder.call_llm", return_value=ready_reply):
        res = create_session_with_idea("My idea", db_path=test_db)
        session_id = res["session_id"]

    assert get_session(session_id, db_path=test_db) is not None
    assert len(get_messages(session_id, db_path=test_db)) == 2
    assert get_final_prompt(session_id, db_path=test_db) is not None

    deleted = delete_session(session_id, db_path=test_db)
    assert deleted is True
    assert get_session(session_id, db_path=test_db) is None
    assert len(get_messages(session_id, db_path=test_db)) == 0
    assert get_final_prompt(session_id, db_path=test_db) is None


def test_parse_reply_recovers_from_syntax_glitches() -> None:
    """Verify parse_reply recovers from markdown fences, trailing braces, and trailing commas."""
    # Markdown code fence wrapping
    fenced = "```json\n{\"status\":\"ready\",\"type\":\"study\",\"final_prompt\":\"## Role\\nTeacher\"}\n```"
    res = parse_reply(fenced, force_ready=True)
    assert res is not None
    assert res["status"] == "ready"
    assert res["final_prompt"] == "## Role\nTeacher"

    # Trailing brace glitch: ","}}
    glitched_braces = '{"status":"ready","type":"study","final_prompt":"## Role\\nTeacher","}}'
    res2 = parse_reply(glitched_braces, force_ready=True)
    assert res2 is not None
    assert res2["status"] == "ready"
    assert res2["final_prompt"] == "## Role\nTeacher"

    # Trailing comma before closing brace: ,}
    trailing_comma = '{"status":"ready","type":"study","final_prompt":"## Role\\nTeacher",}'
    res3 = parse_reply(trailing_comma, force_ready=True)
    assert res3 is not None
    assert res3["status"] == "ready"


def test_call_llm_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify call_llm includes max_tokens and sets reasoning_effort for gpt-oss models."""
    from unittest.mock import MagicMock
    import services.llm as llm_module

    mock_client = MagicMock()
    mock_client.api_key = "mock_key"
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"status": "ok"}'
    mock_choice.finish_reason = "stop"
    mock_resp.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_resp

    monkeypatch.setenv("GROQ_API_KEY", "mock_key")
    monkeypatch.setattr(llm_module, "_client", mock_client)
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    llm_module.call_llm([{"role": "user", "content": "hi"}], max_tokens=4000)

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs["max_tokens"] == 4000
    assert call_kwargs["reasoning_effort"] == "low"
    assert llm_module.get_last_finish_reason() == "stop"


