"""
Tests for the embeddings module.

Run with: python test_embeddings.py

Tests are split into two groups:
    - Offline tests: verify error handling and validation logic
      (run anywhere, no Ollama needed)
    - Live tests: actually call Ollama to generate embeddings
      (require Ollama running with nomic-embed-text pulled)

The live tests are the important ones — they verify that real
embeddings behave as expected (similar texts produce similar
vectors, unrelated texts produce dissimilar vectors).

If Ollama isn't running, live tests are skipped with a message.
"""

import sys
import numpy as np


def test_empty_text_rejected():
    """get_embedding should reject empty strings."""
    print("  test_empty_text_rejected...", end=" ")

    from embeddings import get_embedding

    try:
        get_embedding("")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass  # Expected

    try:
        get_embedding("   ")
        assert False, "Should have raised ValueError for whitespace"
    except ValueError:
        pass  # Expected

    print("PASS")


def test_embedding_dim_constant():
    """Verify the expected dimension constant is set."""
    print("  test_embedding_dim_constant...", end=" ")

    from embeddings import EMBEDDING_DIM
    assert EMBEDDING_DIM == 768, f"Expected 768, got {EMBEDDING_DIM}"

    print("PASS")


def _ollama_available() -> bool:
    """Check if Ollama is running and the embedding model is available."""
    try:
        import requests
        import config
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={"model": "nomic-embed-text", "input": "test"},
            timeout=5,
        )
        return response.status_code == 200
    except Exception:
        return False


def test_live_basic_embedding():
    """Generate a real embedding and verify its shape and type."""
    print("  test_live_basic_embedding...", end=" ")

    from embeddings import get_embedding, EMBEDDING_DIM

    vector = get_embedding("Hello, how are you?")

    assert isinstance(vector, np.ndarray), f"Expected ndarray, got {type(vector)}"
    assert vector.dtype == np.float32, f"Expected float32, got {vector.dtype}"
    assert vector.shape == (EMBEDDING_DIM,), f"Expected ({EMBEDDING_DIM},), got {vector.shape}"

    # Vector should not be all zeros
    assert np.any(vector != 0), "Embedding is all zeros"

    print("PASS")


def test_live_similar_texts():
    """Similar texts should produce high cosine similarity."""
    print("  test_live_similar_texts...", end=" ")

    from embeddings import get_embedding
    from memory import _cosine_similarity

    # These say roughly the same thing in different words
    v1 = get_embedding("I want to switch careers from teaching to tech.")
    v2 = get_embedding("I'm thinking about leaving education for a software job.")

    similarity = _cosine_similarity(v1, v2)
    assert similarity > 0.5, (
        f"Expected high similarity for related texts, got {similarity:.3f}"
    )

    print(f"PASS (similarity: {similarity:.3f})")


def test_live_unrelated_texts():
    """Unrelated texts should produce low cosine similarity."""
    print("  test_live_unrelated_texts...", end=" ")

    from embeddings import get_embedding
    from memory import _cosine_similarity

    v1 = get_embedding("I want to switch careers from teaching to tech.")
    v2 = get_embedding("The recipe calls for two cups of flour and one egg.")

    similarity = _cosine_similarity(v1, v2)
    assert similarity < 0.4, (
        f"Expected low similarity for unrelated texts, got {similarity:.3f}"
    )

    print(f"PASS (similarity: {similarity:.3f})")


def test_live_deterministic():
    """Same input should produce the same embedding."""
    print("  test_live_deterministic...", end=" ")

    from embeddings import get_embedding

    text = "This is a test of deterministic embeddings."
    v1 = get_embedding(text)
    v2 = get_embedding(text)

    # Should be identical (or extremely close due to float precision)
    diff = np.max(np.abs(v1 - v2))
    assert diff < 1e-6, f"Same text produced different embeddings (max diff: {diff})"

    print("PASS")


if __name__ == "__main__":
    print("\nRunning embedding tests...\n")

    # Offline tests — always run
    offline_tests = [
        test_empty_text_rejected,
        test_embedding_dim_constant,
    ]

    # Live tests — only run if Ollama is available
    live_tests = [
        test_live_basic_embedding,
        test_live_similar_texts,
        test_live_unrelated_texts,
        test_live_deterministic,
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
            f"  Skipping {skipped} live tests — Ollama not available.\n"
            f"  To run them: ollama serve && ollama pull nomic-embed-text\n"
        )

    print(f"\n{'='*40}")
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"  {', '.join(parts)}")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
