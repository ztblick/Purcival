"""
Tests for context assembly (context.py).

Verifies that:
    1. System prompt includes persona and user context sections
    2. Empty sections are omitted cleanly
    3. Token budgets are enforced via truncation
    4. Messages are windowed by token count, not message count
    5. Messages are returned in chronological order
    6. The output format matches what brain.ask() expects

Run with: python test_context.py
"""

import sys
import shutil
from pathlib import Path

import memory
import context
from tokens import get_token_count

# Use test directories
TEST_DATA_DIR = Path(__file__).parent / "test_data_context"
memory.DATA_DIR = TEST_DATA_DIR
context.DATA_DIR = TEST_DATA_DIR
context.USER_CONTEXT_PATH = TEST_DATA_DIR / "user_context.md"


def cleanup():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def setup_user_context(content: str = ""):
    """Create a user_context.md in the test directory."""
    memory.DATA_DIR = TEST_DATA_DIR
    context.DATA_DIR = TEST_DATA_DIR
    context.USER_CONTEXT_PATH = TEST_DATA_DIR / "user_context.md"
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context.USER_CONTEXT_PATH.write_text(content)


def test_basic_assembly():
    """System prompt should contain persona and user context sections."""
    print("  test_basic_assembly...", end=" ")

    setup_user_context("Zach is a teacher who lives in Menlo Park.")

    mem = memory.PersonaMemory("test_basic")
    mem.add_message("user", "Hello there")
    mem.add_message("assistant", "Hi! How can I help?")

    persona_prompt = "You are a helpful assistant named TestBot."
    system, messages = context.assemble_context(persona_prompt, mem)

    # System prompt should contain both sections
    assert "PERSONA" in system
    assert "TestBot" in system
    assert "ABOUT THE USER" in system
    assert "Menlo Park" in system

    # Messages should be formatted for brain.ask()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert "Hello there" in messages[0]["content"]
    assert messages[1] == {"role": "assistant", "content": "Hi! How can I help?"}

    # User messages should have timestamps prepended
    assert messages[0]["content"].startswith("[")

    # System prompt should include current session info
    assert "CURRENT SESSION" in system

    print("PASS")


def test_missing_user_context():
    """If user_context.md doesn't exist, that section should be skipped."""
    print("  test_missing_user_context...", end=" ")

    # Point to a nonexistent file
    context.USER_CONTEXT_PATH = TEST_DATA_DIR / "nonexistent.md"

    mem = memory.PersonaMemory("test_no_context")
    persona_prompt = "You are TestBot."
    system, messages = context.assemble_context(persona_prompt, mem)

    assert "PERSONA" in system
    assert "ABOUT THE USER" not in system

    # Restore
    context.USER_CONTEXT_PATH = TEST_DATA_DIR / "user_context.md"

    print("PASS")


def test_empty_summaries_and_additional():
    """Placeholder sections (summaries, additional) should be omitted."""
    print("  test_empty_summaries_and_additional...", end=" ")

    setup_user_context("Some context.")
    mem = memory.PersonaMemory("test_empty_sections")
    persona_prompt = "You are TestBot."

    system, messages = context.assemble_context(persona_prompt, mem)

    # These sections have no content yet (placeholders return "")
    assert "RELEVANT PAST CONVERSATIONS" not in system
    assert "ADDITIONAL CONTEXT" not in system

    print("PASS")


def test_truncation():
    """Sections exceeding their budget should be truncated."""
    print("  test_truncation...", end=" ")

    # Create user context that's way over the 2,000 token budget
    # 2,000 tokens ≈ 8,000 chars. Let's make 20,000 chars.
    huge_context = "Important fact. " * 1250  # ~20,000 chars
    setup_user_context(huge_context)

    mem = memory.PersonaMemory("test_truncation")
    persona_prompt = "You are TestBot."

    system, messages = context.assemble_context(persona_prompt, mem)

    # The user context section should be truncated
    assert "truncated" in system.lower()

    # The total user context portion should be within budget
    # (we can't easily isolate the section, but the truncation marker
    # proves it was enforced)

    print("PASS")


def test_message_windowing_by_tokens():
    """Messages should be selected by token count, not message count."""
    print("  test_message_windowing_by_tokens...", end=" ")

    mem = memory.PersonaMemory("test_windowing")

    # Add a bunch of short messages
    for i in range(100):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(role, f"Short message {i}")

    persona_prompt = "You are TestBot."
    system, messages = context.assemble_context(persona_prompt, mem)

    # All 100 short messages should fit within 32,000 tokens
    assert len(messages) == 100

    # Now add some very long messages that will push us over budget
    long_text = "x " * 50_000  # ~25,000 tokens
    mem.add_message("user", long_text)
    mem.add_message("assistant", "Got it.")

    system, messages = context.assemble_context(persona_prompt, mem)

    # Should include the long message and the short reply,
    # but NOT all 100 previous short messages (budget exceeded)
    total_tokens = sum(get_token_count(m["content"]) for m in messages)
    assert total_tokens <= context.BUDGET_MESSAGES + 1000  # some slack for approximation

    # Messages should be in chronological order
    assert messages[-1]["content"] == "Got it."

    print("PASS")


def test_chronological_order():
    """Messages must be oldest-first for the LLM."""
    print("  test_chronological_order...", end=" ")

    mem = memory.PersonaMemory("test_order")
    mem.add_message("user", "First")
    mem.add_message("assistant", "Second")
    mem.add_message("user", "Third")

    persona_prompt = "You are TestBot."
    system, messages = context.assemble_context(persona_prompt, mem)

    assert "First" in messages[0]["content"]
    assert messages[1]["content"] == "Second"  # assistant, no timestamp
    assert "Third" in messages[2]["content"]

    print("PASS")


def test_separator_formatting():
    """Sections should be separated by --- dividers."""
    print("  test_separator_formatting...", end=" ")

    setup_user_context("User info here.")
    mem = memory.PersonaMemory("test_separators")
    persona_prompt = "You are TestBot."

    system, messages = context.assemble_context(persona_prompt, mem)

    # Should have a separator between persona and user context
    assert "---" in system

    print("PASS")


def test_token_count_utility():
    """Verify the token count function works as expected."""
    print("  test_token_count_utility...", end=" ")

    assert get_token_count("") == 0
    assert get_token_count("hello") == 1  # 5 chars / 4 = 1
    assert get_token_count("a" * 400) == 100  # 400 / 4 = 100

    print("PASS")


if __name__ == "__main__":
    cleanup()

    print("\nRunning context assembly tests...\n")

    tests = [
        test_basic_assembly,
        test_missing_user_context,
        test_empty_summaries_and_additional,
        test_truncation,
        test_message_windowing_by_tokens,
        test_chronological_order,
        test_separator_formatting,
        test_token_count_utility,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL — {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    cleanup()

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
