"""
Tests for summary retrieval in context assembly.

Run with: python test_retrieval.py

These tests verify the complete memory loop:
    - Summaries with embeddings are found via semantic search
    - Similarity threshold filters out irrelevant summaries
    - Recent summaries are always included for continuity
    - Summaries appear in the assembled system prompt
    - The full pipeline works: store summary → embed → retrieve → assemble

Offline tests use synthetic embeddings (no Ollama needed).
Live tests require Ollama with nomic-embed-text.
"""

import sys
import shutil
import numpy as np
from pathlib import Path

import memory
import context
from tokens import get_token_count

# Use test directories
TEST_DATA_DIR = Path(__file__).parent / "test_data_retrieval"
memory.DATA_DIR = TEST_DATA_DIR
context.DATA_DIR = TEST_DATA_DIR
context.USER_CONTEXT_PATH = TEST_DATA_DIR / "user_context.md"


def cleanup():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def _reset_paths():
    memory.DATA_DIR = TEST_DATA_DIR
    context.DATA_DIR = TEST_DATA_DIR
    context.USER_CONTEXT_PATH = TEST_DATA_DIR / "user_context.md"


def setup_module():
    cleanup()
    _reset_paths()


def teardown_module():
    cleanup()


def setup():
    """Create test data directory and user context."""
    _reset_paths()
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context.USER_CONTEXT_PATH.write_text("Test user context.")


def test_summaries_appear_in_prompt():
    """When summaries exist and match, they appear in the system prompt."""
    print("  test_summaries_appear_in_prompt...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_retrieval")

    # Add a message so there's a current_message to query with
    mem.add_message("user", "Tell me about career changes.")

    # Add a summary with an embedding that will match "career changes"
    # We'll use a synthetic embedding — point it in a known direction
    emb = np.zeros(768, dtype=np.float32)
    emb[0] = 1.0  # Arbitrary direction
    mem.add_summary(
        summary="Discussed switching from teaching to tech. User mentioned salary concerns.",
        message_start=1,
        message_end=10,
        embedding=emb,
    )

    # Mock the embedding function to return a vector in the same direction
    import embeddings
    original_fn = embeddings.get_embedding
    embeddings.get_embedding = lambda text: emb.copy()

    try:
        system, messages = context.assemble_context("You are TestBot.", mem)
        assert "RELEVANT PAST CONVERSATIONS" in system
        assert "teaching to tech" in system
        assert "salary" in system
    finally:
        embeddings.get_embedding = original_fn

    print("PASS")


def test_low_similarity_filtered_out():
    """Summaries below the similarity threshold should be excluded."""
    print("  test_low_similarity_filtered_out...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_filter")
    mem.add_message("user", "What's for dinner?")

    # Add a summary with embedding pointing in a DIFFERENT direction
    emb_stored = np.zeros(768, dtype=np.float32)
    emb_stored[0] = 1.0

    mem.add_summary(
        summary="Discussed quantum physics and the nature of reality.",
        message_start=1,
        message_end=10,
        embedding=emb_stored,
    )

    # Query embedding points in a perpendicular direction (low similarity)
    emb_query = np.zeros(768, dtype=np.float32)
    emb_query[5] = 1.0  # Orthogonal to stored embedding

    import embeddings
    original_fn = embeddings.get_embedding
    embeddings.get_embedding = lambda text: emb_query

    try:
        # Temporarily disable recent-always to test pure similarity filtering
        original_recent = context.SUMMARY_ALWAYS_RECENT
        context.SUMMARY_ALWAYS_RECENT = 0

        system, messages = context.assemble_context("You are TestBot.", mem)

        # The quantum physics summary should NOT appear (similarity ~0)
        assert "quantum physics" not in system

        context.SUMMARY_ALWAYS_RECENT = original_recent
    finally:
        embeddings.get_embedding = original_fn

    print("PASS")


def test_recent_summaries_always_included():
    """Most recent summaries should appear even if similarity is low."""
    print("  test_recent_summaries_always_included...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_recent")
    mem.add_message("user", "Something completely unrelated.")

    # Add an old summary
    emb1 = np.zeros(768, dtype=np.float32)
    emb1[0] = 1.0
    mem.add_summary("Old conversation about cooking.", 1, 5, emb1)

    # Add a recent summary
    emb2 = np.zeros(768, dtype=np.float32)
    emb2[1] = 1.0
    mem.add_summary("Recent discussion about the Purcival project.", 6, 10, emb2)

    # Query points somewhere unrelated to both
    emb_query = np.zeros(768, dtype=np.float32)
    emb_query[99] = 1.0

    import embeddings
    original_fn = embeddings.get_embedding
    embeddings.get_embedding = lambda text: emb_query

    try:
        system, messages = context.assemble_context("You are TestBot.", mem)

        # The recent summary should appear (SUMMARY_ALWAYS_RECENT = 2)
        assert "Purcival project" in system

    finally:
        embeddings.get_embedding = original_fn

    print("PASS")


def test_deduplication():
    """A summary found by both search and recency should appear only once."""
    print("  test_deduplication...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_dedup")
    mem.add_message("user", "Let's discuss careers.")

    # One summary that will match by similarity AND be the most recent
    emb = np.zeros(768, dtype=np.float32)
    emb[0] = 1.0
    mem.add_summary("Career change discussion.", 1, 10, emb)

    import embeddings
    original_fn = embeddings.get_embedding
    embeddings.get_embedding = lambda text: emb.copy()

    try:
        system, messages = context.assemble_context("You are TestBot.", mem)

        # Should appear exactly once
        count = system.count("Career change discussion.")
        assert count == 1, f"Summary appeared {count} times, expected 1"

    finally:
        embeddings.get_embedding = original_fn

    print("PASS")


def test_no_summaries_no_section():
    """If no summaries exist at all, the section should be absent."""
    print("  test_no_summaries_no_section...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_empty")
    mem.add_message("user", "Hello")

    system, messages = context.assemble_context("You are TestBot.", mem)
    assert "RELEVANT PAST CONVERSATIONS" not in system

    print("PASS")


def test_embedding_failure_graceful():
    """If embedding fails, retrieval should degrade to recent-only."""
    print("  test_embedding_failure_graceful...", end=" ")
    setup()

    mem = memory.PersonaMemory("test_graceful")
    mem.add_message("user", "Test message")

    # Add a summary (without embedding — simulates failed embedding at storage time)
    mem.add_summary("A conversation that had no embedding.", 1, 5, None)

    # Add another summary WITH an embedding
    emb = np.zeros(768, dtype=np.float32)
    emb[0] = 1.0
    mem.add_summary("A conversation with a valid embedding.", 6, 10, emb)

    # Make get_embedding raise an error
    import embeddings
    original_fn = embeddings.get_embedding
    def failing_embed(text):
        raise RuntimeError("Ollama not available")
    embeddings.get_embedding = failing_embed

    try:
        # Should not crash — falls back to recent summaries
        system, messages = context.assemble_context("You are TestBot.", mem)

        # Recent summaries should still appear (the one with embedding)
        assert "valid embedding" in system

    finally:
        embeddings.get_embedding = original_fn

    print("PASS")


def _ollama_available() -> bool:
    try:
        import requests
        import config
        r = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={"model": "nomic-embed-text", "input": "test"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def test_live_end_to_end_retrieval():
    """Full pipeline: store real summaries with real embeddings, retrieve them."""
    print("  test_live_end_to_end_retrieval...", end=" ")
    setup()

    from embeddings import get_embedding

    mem = memory.PersonaMemory("test_live_e2e")

    # Store three summaries about different topics with real embeddings
    topics = [
        "Discussed career transition from teaching to software engineering. "
        "User is concerned about salary gap and learning curve.",
        "Talked about weekend cooking plans. User wants to try making "
        "sourdough bread and a Thai curry recipe.",
        "Reviewed the Purcival AI assistant project architecture. Discussed "
        "database schema for persistent memory and embedding retrieval.",
    ]

    for i, topic in enumerate(topics):
        emb = get_embedding(topic)
        mem.add_summary(topic, i*10+1, (i+1)*10, emb)

    # Now ask about career stuff — should retrieve the career summary
    mem.add_message("user", "I've been thinking more about switching to tech.")

    system, messages = context.assemble_context("You are TestBot.", mem)

    assert "RELEVANT PAST CONVERSATIONS" in system
    assert "career" in system.lower() or "teaching" in system.lower(), (
        "Career summary should be retrieved for a career-related query"
    )

    # Verify relevance scores appear
    assert "relevance:" in system

    print("PASS")


if __name__ == "__main__":
    cleanup()

    print("\nRunning retrieval tests...\n")

    offline_tests = [
        test_summaries_appear_in_prompt,
        test_low_similarity_filtered_out,
        test_recent_summaries_always_included,
        test_deduplication,
        test_no_summaries_no_section,
        test_embedding_failure_graceful,
    ]

    live_tests = [
        test_live_end_to_end_retrieval,
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
            f"  To run: ollama pull nomic-embed-text\n"
        )

    cleanup()

    print(f"\n{'='*40}")
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"  {', '.join(parts)}")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
