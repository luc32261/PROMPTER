"""Demonstration script showing the end-to-end flow with a sample PDF."""
import os
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from db import get_messages, get_session, init_db
from services.builder import advance_session, create_session_with_idea
from services.extractor import extract_text_from_file

def main() -> None:
    print("==================================================")
    print("PROMPTER: Sample PDF Flow Demonstration")
    print("==================================================")

    # 1. Load sample PDF
    pdf_path = BASE_DIR / "tests" / "sample_math_syllabus.pdf"
    if not pdf_path.exists():
        print(f"Error: {pdf_path} not found!")
        return

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    print(f"\n[1] Uploaded File: {pdf_path.name} ({len(pdf_bytes)} bytes)")
    extracted = extract_text_from_file(pdf_bytes, pdf_path.name)
    print(f"    Extracted Text: \"{extracted}\"")

    # 2. Start session with idea and PDF attachment
    idea = "Help me prepare a comprehensive study guide for this course"
    print(f"\n[2] User Idea: \"{idea}\"")
    print("    Starting session with attached PDF context...")

    demo_db = BASE_DIR / "data" / "demo.db"
    init_db(demo_db)

    result_turn1 = create_session_with_idea(
        idea=idea,
        attachment=(pdf_path.name, pdf_bytes),
        db_path=demo_db,
    )

    session_id = result_turn1["session_id"]
    print(f"\n[3] Turn 1 Result (Session ID: {session_id}):")
    print(f"    Status: {result_turn1.get('status')}")
    print(f"    Type: {result_turn1.get('type')}")
    print(f"    Has Attachment: {result_turn1.get('has_attachment')}")
    print(f"    Attachment Filename: {result_turn1.get('attachment_filename')}")
    if result_turn1.get("status") == "ask":
        print(f"    Assistant Question: \"{result_turn1.get('question')}\"")
    elif result_turn1.get("status") == "ready":
        print("    Final Prompt Generated Directly:")
        print(result_turn1.get("final_prompt"))

    # 3. If assistant asked a question, provide answer
    if result_turn1.get("status") == "ask":
        answer = "This is for college undergraduates; focus on Stokes' Theorem and surface integrals with practice problems."
        print(f"\n[4] User Answer: \"{answer}\"")
        print("    Advancing session (attachment context is automatically retained in system prompt)...")

        result_turn2 = advance_session(session_id, text=answer, db_path=demo_db)
        print(f"\n[5] Turn 2 Result:")
        print(f"    Status: {result_turn2.get('status')}")
        print(f"    Type: {result_turn2.get('type')}")
        print(f"    Has Attachment: {result_turn2.get('has_attachment')}")
        if result_turn2.get("status") == "ready":
            print("\n    >>> FINAL PROMPT GENERATED <<<")
            print("--------------------------------------------------")
            print(result_turn2.get("final_prompt"))
            print("--------------------------------------------------")
        else:
            print(f"    Assistant Question: \"{result_turn2.get('question')}\"")

    # 4. Verify session record in database
    session_row = get_session(session_id, db_path=demo_db)
    print("\n[6] Database Verification:")
    print(f"    session.has_attachment = {session_row['has_attachment']}")
    print(f"    session.attachment_filename = {session_row['attachment_filename']}")
    print(f"    session.attachment_text = \"{session_row['attachment_text']}\"")
    print("\nFlow completed successfully!")

if __name__ == "__main__":
    main()
