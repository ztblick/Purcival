"""
Integration test for Step 2 — message persistence.

Verifies that:
    1. Messages are stored when added through PersonaMemory
    2. Messages survive when a new PersonaMemory instance is created
       (simulating a process restart)
    3. The history format matches what brain.ask() expects
    4. Different personas have completely isolated databases
    5. /clear wipes the correct persona without affecting others

Run with: python test_persistence.py
"""

import sys
import shutil
from pathlib import Path

import memory

# Use a test directory to avoid touching real data
TEST_DATA_DIR = Path(__file__).parent / "test_data_integration"
memory.DATA_DIR = TEST_DATA_DIR


def cleanup():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def _reset_data_dir():
    memory.DATA_DIR = TEST_DATA_DIR


def setup_module():
    cleanup()
    _reset_data_dir()


def teardown_module():
    cleanup()


def test_messages_survive_restart():
    """Simulate a process restart and verify messages persist."""
    print("  test_messages_survive_restart...", end=" ")

    # First "session" — create memory, add messages
    mem1 = memory.PersonaMemory("purcival")
    mem1.add_message("user", "What should I work on today?")
    mem1.add_message("assistant", "Let's review your priorities.")
    mem1.add_message("user", "Good idea. My main project is the assistant.")
    mem1.add_message("assistant", "How's the memory system coming along?")

    # Simulate restart — create a NEW instance for the same persona
    mem2 = memory.PersonaMemory("purcival")

    recent = mem2.get_recent_messages(limit=10)
    assert len(recent) == 4, f"Expected 4 messages after restart, got {len(recent)}"
    assert recent[0]["content"] == "What should I work on today?"
    assert recent[3]["content"] == "How's the memory system coming along?"

    print("PASS")


def test_history_format_for_brain():
    """Verify the history can be formatted for brain.ask()."""
    print("  test_history_format_for_brain...", end=" ")

    mem = memory.PersonaMemory("purcival")
    recent = mem.get_recent_messages(limit=10)

    # Format like the bot does before calling brain.ask()
    history = [{"role": m["role"], "content": m["content"]} for m in recent]

    assert len(history) == 4
    assert all(set(m.keys()) == {"role", "content"} for m in history)
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"

    print("PASS")


def test_persona_isolation():
    """Verify that two personas have completely separate memories."""
    print("  test_persona_isolation...", end=" ")

    mem_p = memory.PersonaMemory("purcival")
    mem_j = memory.PersonaMemory("ada")

    # Purcival already has 4 messages from the earlier test
    # Add a message to Ada
    mem_j.add_message("user", "Schedule a meeting for tomorrow.")
    mem_j.add_message("assistant", "Done — 10am with the team.")

    assert mem_p.get_message_count() == 4, "Purcival should still have 4"
    assert mem_j.get_message_count() == 2, "Ada should have 2"

    # Verify content doesn't leak
    p_messages = mem_p.get_recent_messages(limit=10)
    j_messages = mem_j.get_recent_messages(limit=10)

    p_contents = [m["content"] for m in p_messages]
    j_contents = [m["content"] for m in j_messages]

    assert "Schedule a meeting for tomorrow." not in p_contents
    assert "What should I work on today?" not in j_contents

    print("PASS")


def test_clear_only_affects_target_persona():
    """Clearing one persona should not affect others."""
    print("  test_clear_only_affects_target_persona...", end=" ")

    mem_j = memory.PersonaMemory("ada")
    mem_p = memory.PersonaMemory("purcival")

    # Clear Ada
    mem_j.clear_history()

    assert mem_j.get_message_count() == 0, "Ada should be empty"
    assert mem_p.get_message_count() == 4, "Purcival should be untouched"

    print("PASS")


def test_single_message_mode_persists():
    """Simulate single-message mode (-m flag) and verify persistence."""
    print("  test_single_message_mode_persists...", end=" ")

    mem = memory.PersonaMemory("default")

    # Simulate what main.py single_message() does
    mem.add_message("user", "What time is it?")
    # (brain.ask would be called here in real code)
    mem.add_message("assistant", "I don't have access to a clock.")

    # Later, another single message
    mem.add_message("user", "Fair enough. What can you help with?")
    mem.add_message("assistant", "I can help with thinking through problems.")

    # Both interactions should be in history
    recent = mem.get_recent_messages(limit=10)
    assert len(recent) == 4
    assert recent[0]["content"] == "What time is it?"
    assert recent[3]["content"] == "I can help with thinking through problems."

    print("PASS")


def test_recent_messages_limit():
    """Verify that limit correctly caps the returned messages."""
    print("  test_recent_messages_limit...", end=" ")

    mem = memory.PersonaMemory("test_limit")

    # Add 50 messages
    for i in range(50):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(role, f"Message number {i}")

    # Requesting 40 should return the 40 most recent
    recent = mem.get_recent_messages(limit=40)
    assert len(recent) == 40
    # First returned message should be #10 (the 11th message, 0-indexed)
    assert recent[0]["content"] == "Message number 10"
    # Last should be #49
    assert recent[-1]["content"] == "Message number 49"

    print("PASS")


if __name__ == "__main__":
    cleanup()

    print("\nRunning persistence integration tests...\n")

    tests = [
        test_messages_survive_restart,
        test_history_format_for_brain,
        test_persona_isolation,
        test_clear_only_affects_target_persona,
        test_single_message_mode_persists,
        test_recent_messages_limit,
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
