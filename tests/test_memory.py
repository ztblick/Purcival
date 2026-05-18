"""
Tests for the memory module.

Run with: python test_memory.py

Tests the full lifecycle: creating databases, storing messages,
storing summaries with embeddings, and retrieving by similarity.
"""

import sys
import shutil
import numpy as np
from pathlib import Path

# We need to test with a temporary data directory so we don't
# pollute any real persona data.
import memory

# Override DATA_DIR for testing
TEST_DATA_DIR = Path(__file__).parent / "test_data"
memory.DATA_DIR = TEST_DATA_DIR


def cleanup():
    """Remove test data directory."""
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def _reset_data_dir():
    memory.DATA_DIR = TEST_DATA_DIR


def setup_module():
    cleanup()
    _reset_data_dir()


def teardown_module():
    cleanup()


def test_create_and_store_messages():
    """Test basic message storage and retrieval."""
    print("  test_create_and_store_messages...", end=" ")

    mem = memory.PersonaMemory("test_persona")

    # Store some messages
    id1 = mem.add_message("user", "Hello, how are you?")
    id2 = mem.add_message("assistant", "I'm doing well! How can I help?")
    id3 = mem.add_message("user", "Tell me about Python.")
    id4 = mem.add_message("assistant", "Python is a programming language...")

    assert id1 == 1, f"Expected id 1, got {id1}"
    assert id4 == 4, f"Expected id 4, got {id4}"

    # Retrieve recent messages
    recent = mem.get_recent_messages(limit=10)
    assert len(recent) == 4, f"Expected 4 messages, got {len(recent)}"
    assert recent[0]["role"] == "user"
    assert recent[0]["content"] == "Hello, how are you?"
    assert recent[-1]["role"] == "assistant"

    # Test limit
    recent_2 = mem.get_recent_messages(limit=2)
    assert len(recent_2) == 2, f"Expected 2 messages, got {len(recent_2)}"
    # Should be the LAST two messages, in chronological order
    assert recent_2[0]["content"] == "Tell me about Python."
    assert recent_2[1]["content"] == "Python is a programming language..."

    # Test count
    assert mem.get_message_count() == 4

    print("PASS")


def test_messages_since():
    """Test retrieving messages after a given ID."""
    print("  test_messages_since...", end=" ")

    mem = memory.PersonaMemory("test_persona")
    # DB already has 4 messages from previous test

    messages = mem.get_messages_since(2)
    assert len(messages) == 2, f"Expected 2 messages after id 2, got {len(messages)}"
    assert messages[0]["id"] == 3
    assert messages[1]["id"] == 4

    print("PASS")


def test_summaries():
    """Test summary storage and retrieval."""
    print("  test_summaries...", end=" ")

    mem = memory.PersonaMemory("test_persona")

    # Create a fake embedding (768-dim like nomic-embed-text)
    fake_embedding = np.random.randn(768).astype(np.float32)

    summary_id = mem.add_summary(
        summary="User greeted the assistant and asked about Python.",
        message_start=1,
        message_end=4,
        embedding=fake_embedding,
    )
    assert summary_id == 1

    # Check last summarized ID
    assert mem.get_last_summarized_id() == 4

    # Get all summaries
    all_summaries = mem.get_all_summaries()
    assert len(all_summaries) == 1
    assert "Python" in all_summaries[0]["summary"]

    print("PASS")


def test_similarity_search():
    """Test that cosine similarity search returns relevant results."""
    print("  test_similarity_search...", end=" ")

    mem = memory.PersonaMemory("test_search")

    # Create embeddings that point in known directions
    # Summary 1: points mostly along dimension 0
    emb1 = np.zeros(768, dtype=np.float32)
    emb1[0] = 1.0
    emb1[1] = 0.1
    mem.add_summary("Discussed career goals and job hunting.", 1, 10, emb1)

    # Summary 2: points mostly along dimension 1
    emb2 = np.zeros(768, dtype=np.float32)
    emb2[0] = 0.1
    emb2[1] = 1.0
    mem.add_summary("Talked about Python programming techniques.", 11, 20, emb2)

    # Summary 3: points along dimension 2 (unrelated to query)
    emb3 = np.zeros(768, dtype=np.float32)
    emb3[2] = 1.0
    mem.add_summary("Discussed weekend cooking plans.", 21, 30, emb3)

    # Query that's similar to summary 1 (career-related)
    query = np.zeros(768, dtype=np.float32)
    query[0] = 1.0
    query[1] = 0.2

    results = mem.search_summaries(query, top_k=2, embedding_dim=768)
    assert len(results) == 2
    assert "career" in results[0]["summary"], (
        f"Expected career summary first, got: {results[0]['summary']}"
    )
    assert results[0]["similarity"] > results[1]["similarity"]

    print("PASS")


def test_unsummarized_messages():
    """Test tracking of which messages still need summarization."""
    print("  test_unsummarized_messages...", end=" ")

    mem = memory.PersonaMemory("test_unsummarized")

    # Add 10 messages
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(role, f"Message {i}")

    # Nothing summarized yet — all 10 should be unsummarized
    unsummarized = mem.get_unsummarized_messages()
    assert len(unsummarized) == 10

    # Summarize the first 6
    mem.add_summary("Summary of messages 1-6", 1, 6, None)

    # Now only 4 should be unsummarized (IDs 7-10)
    unsummarized = mem.get_unsummarized_messages()
    assert len(unsummarized) == 4, f"Expected 4, got {len(unsummarized)}"
    assert unsummarized[0]["id"] == 7

    print("PASS")


def test_clear_history():
    """Test that clear wipes messages and summaries."""
    print("  test_clear_history...", end=" ")

    mem = memory.PersonaMemory("test_clear")
    mem.add_message("user", "Hello")
    mem.add_message("assistant", "Hi")
    mem.add_summary("Greeting exchange", 1, 2, None)

    assert mem.get_message_count() == 2
    assert len(mem.get_all_summaries()) == 1

    mem.clear_history()

    assert mem.get_message_count() == 0
    assert len(mem.get_all_summaries()) == 0

    print("PASS")


def test_separate_persona_databases():
    """Verify that different personas get different databases."""
    print("  test_separate_persona_databases...", end=" ")

    mem_a = memory.PersonaMemory("persona_alpha")
    mem_b = memory.PersonaMemory("persona_beta")

    mem_a.add_message("user", "Message for alpha")
    mem_b.add_message("user", "Message for beta")

    # Each should only see their own messages
    assert mem_a.get_message_count() == 1
    assert mem_b.get_message_count() == 1
    assert mem_a.get_recent_messages()[0]["content"] == "Message for alpha"
    assert mem_b.get_recent_messages()[0]["content"] == "Message for beta"

    # Databases should be in separate directories
    assert mem_a.db_path != mem_b.db_path
    assert mem_a.db_path.parent.name == "persona_alpha"
    assert mem_b.db_path.parent.name == "persona_beta"

    print("PASS")


def test_invalid_role_rejected():
    """Ensure we can't store messages with invalid roles."""
    print("  test_invalid_role_rejected...", end=" ")

    mem = memory.PersonaMemory("test_validation")
    try:
        mem.add_message("system", "This should fail")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass  # Expected

    print("PASS")


if __name__ == "__main__":
    cleanup()

    print("\nRunning memory module tests...\n")

    tests = [
        test_create_and_store_messages,
        test_messages_since,
        test_summaries,
        test_similarity_search,
        test_unsummarized_messages,
        test_clear_history,
        test_separate_persona_databases,
        test_invalid_role_rejected,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL — {e}")
            failed += 1

    cleanup()

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
