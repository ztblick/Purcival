"""
Tests for the summarizer module.

Run with: python test_summarizer.py

Offline tests verify:
    - Batch selection respects token budgets
    - Threshold detection works correctly
    - Message formatting produces readable transcripts
    - check_and_summarize returns False when below threshold

Live tests (require Ollama) verify:
    - A real summary is generated from test messages
    - The summary is stored with an embedding in the database

If Ollama isn't running, live tests skip gracefully.
"""

import sys
import shutil
from pathlib import Path

import memory
import summarizer
from tokens import get_token_count

# Use test directory
TEST_DATA_DIR = Path(__file__).parent / "test_data_summarizer"
memory.DATA_DIR = TEST_DATA_DIR


def cleanup():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def test_batch_selection_respects_budget():
    """_select_batch should take messages up to the token limit."""
    print("  test_batch_selection_respects_budget...", end=" ")

    messages = [
        {"id": i, "role": "user" if i % 2 == 0 else "assistant",
         "content": f"Message {i} " + "word " * 50}
        for i in range(20)
    ]

    # Small budget — should only take a few messages
    batch = summarizer._select_batch(messages, max_tokens=500)
    total_tokens = sum(get_token_count(m["content"]) for m in batch)

    assert len(batch) > 0, "Batch should not be empty"
    assert len(batch) < 20, "Batch should not include all messages"
    assert total_tokens <= 500 + 100, "Batch exceeded budget by too much"
    # Verify we took from the front (oldest)
    assert batch[0]["id"] == 0

    print("PASS")


def test_batch_takes_at_least_one():
    """Even if the first message exceeds the budget, take it anyway."""
    print("  test_batch_takes_at_least_one...", end=" ")

    messages = [
        {"id": 1, "role": "user", "content": "x " * 5000},  # ~2500 tokens
        {"id": 2, "role": "assistant", "content": "Short reply."},
    ]

    batch = summarizer._select_batch(messages, max_tokens=100)
    assert len(batch) == 1, "Should take at least one message even if over budget"
    assert batch[0]["id"] == 1

    print("PASS")


def test_format_messages():
    """Transcript formatting should be readable with timestamps."""
    print("  test_format_messages...", end=" ")

    messages = [
        {"role": "user", "content": "What should I work on?",
         "created_at": "2025-03-12 10:00:00"},
        {"role": "assistant", "content": "Let's review your priorities.",
         "created_at": "2025-03-12 10:00:05"},
        {"role": "user", "content": "Sounds good."},  # no timestamp
    ]

    transcript = summarizer._format_messages_for_summary(messages)

    assert "[Zach, 2025-03-12 10:00:00]: What should I work on?" in transcript
    assert "[AI, 2025-03-12 10:00:05]: Let's review your priorities." in transcript
    assert "[Zach]: Sounds good." in transcript  # graceful without timestamp

    print("PASS")


def test_no_summarization_below_threshold():
    """check_and_summarize should return False when below threshold."""
    print("  test_no_summarization_below_threshold...", end=" ")

    mem = memory.PersonaMemory("test_below_threshold")

    # Add just a few messages — well below 24,000 tokens
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(role, f"Short message {i}")

    result = summarizer.check_and_summarize(mem)
    assert result is False, "Should not summarize with few messages"
    assert len(mem.get_all_summaries()) == 0

    print("PASS")


def test_threshold_detection():
    """Verify threshold is detected when messages are large enough."""
    print("  test_threshold_detection...", end=" ")

    mem = memory.PersonaMemory("test_threshold")

    # Add messages totaling more than SUMMARIZE_THRESHOLD_TOKENS
    # Each message ~250 tokens, need ~100 messages to hit 24,000
    for i in range(120):
        role = "user" if i % 2 == 0 else "assistant"
        # ~1000 chars ≈ 250 tokens each
        content = f"Message {i}: " + "This is a test message with some content. " * 20
        mem.add_message(role, content)

    unsummarized = mem.get_unsummarized_messages()
    total_tokens = sum(get_token_count(m["content"]) for m in unsummarized)

    assert total_tokens > summarizer.SUMMARIZE_THRESHOLD_TOKENS, (
        f"Expected to exceed threshold, got {total_tokens} tokens"
    )

    print("PASS")


def _ollama_available() -> bool:
    """Check if Ollama is running with both models needed."""
    try:
        import requests
        import config
        # Check embedding model
        r1 = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={"model": "nomic-embed-text", "input": "test"},
            timeout=5,
        )
        # Check chat model
        r2 = requests.post(
            f"{config.OLLAMA_BASE_URL}/v1/chat/completions",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [{"role": "user", "content": "test"}],
            },
            timeout=30,
        )
        return r1.status_code == 200 and r2.status_code == 200
    except Exception:
        return False


def test_live_full_summarization():
    """End-to-end: generate enough messages, trigger summarization, verify storage."""
    print("  test_live_full_summarization...", end=" ")

    mem = memory.PersonaMemory("test_live_summarize")

    # Create a realistic conversation that exceeds the threshold.
    # We'll use repetitive but varied content.
    topics = [
        "I've been thinking about switching from teaching to tech. The salary difference is significant.",
        "That's a big decision. What aspects of tech interest you most?",
        "I like building things. This Purcival project has shown me I enjoy software development.",
        "Building projects is a great way to learn. What specific areas appeal to you?",
        "AI and machine learning are fascinating. I also like systems design.",
        "Those are complementary skills. Have you considered any specific companies?",
        "Tessa thinks I should look at startups. She says they'd value my teaching background.",
        "Teaching gives you strong communication skills. That's genuinely valued in tech.",
        "I'm also worried about leaving a stable job. Teaching has good benefits.",
        "That's a valid concern. Have you calculated the financial transition plan?",
    ]

    # Repeat topics with variation to build up volume
    for cycle in range(15):
        for i, topic in enumerate(topics):
            role = "user" if i % 2 == 0 else "assistant"
            content = f"{topic} (Conversation cycle {cycle}, point {i})"
            mem.add_message(role, content)

    # Now trigger summarization
    result = summarizer.check_and_summarize(mem)
    assert result is True, "Summarization should have triggered"

    # Verify a summary was stored
    summaries = mem.get_all_summaries()
    assert len(summaries) >= 1, f"Expected at least 1 summary, got {len(summaries)}"

    summary = summaries[0]
    assert len(summary["summary"]) > 50, "Summary seems too short"
    assert summary["message_start"] == 1, "Should start from message 1"
    assert summary["message_end"] > 1, "Should cover multiple messages"

    # Verify embedding was stored by doing a search
    from embeddings import get_embedding
    query_vec = get_embedding("career change from teaching to tech")
    results = mem.search_summaries(query_vec, top_k=1)
    assert len(results) == 1, "Should find the summary via search"
    assert results[0]["similarity"] > 0.3, (
        f"Summary should be relevant to career query, got {results[0]['similarity']:.3f}"
    )

    print(f"PASS (similarity: {results[0]['similarity']:.3f})")


if __name__ == "__main__":
    cleanup()

    print("\nRunning summarizer tests...\n")

    offline_tests = [
        test_batch_selection_respects_budget,
        test_batch_takes_at_least_one,
        test_format_messages,
        test_no_summarization_below_threshold,
        test_threshold_detection,
    ]

    live_tests = [
        test_live_full_summarization,
    ]

    passed = 0
    failed = 0
    skipped = 0

    print("  Offline tests:\n")
    for test in offline_tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL — {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()

    if _ollama_available():
        print("  Live tests (Ollama detected):\n")
        for test in live_tests:
            try:
                test()
                passed += 1
            except Exception as e:
                print(f"FAIL — {e}")
                import traceback
                traceback.print_exc()
                failed += 1
    else:
        skipped = len(live_tests)
        print(
            f"  Skipping {skipped} live test(s) — Ollama not available.\n"
            f"  To run: ollama serve (with both {summarizer.SUMMARIZE_PROVIDER} "
            f"model and nomic-embed-text pulled)\n"
        )

    cleanup()

    print(f"\n{'='*40}")
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"  {', '.join(parts)}")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)