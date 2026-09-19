import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.llm import call_llm


def run_try_brain(idea: str) -> str:
    """Send an idea to call_llm using brain.txt as the system prompt and return the response."""
    prompt_path = Path(__file__).parent.parent / "prompts" / "brain.txt"
    brain_prompt = prompt_path.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": brain_prompt},
        {"role": "user", "content": idea},
    ]
    return call_llm(messages, json_mode=True)


def main() -> None:
    """CLI entrypoint for testing brain prompt with an idea."""
    if len(sys.argv) < 2:
        print("Usage: python scripts/try_brain.py '<idea>'")
        sys.exit(1)

    idea = " ".join(sys.argv[1:])
    result = run_try_brain(idea)
    print(result)


if __name__ == "__main__":
    main()
