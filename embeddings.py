"""
Embeddings — generate vector representations of text.

Provides a single function for turning text into a vector embedding.
The rest of the app calls get_embedding() and doesn't care how it's
produced. Currently uses nomic-embed-text via Ollama's local API.

The embedding model is small (~270MB) and runs on CPU by default,
so it won't compete with your main LLM for GPU VRAM. It's also fast —
typically under 100ms per embedding.

Setup:
    ollama pull nomic-embed-text

Usage:
    from embeddings import get_embedding
    vector = get_embedding("What should I work on today?")
    # vector is a numpy array of shape (768,)
"""

import requests
import numpy as np
import config

# --- Configuration ---

# The embedding model to use. nomic-embed-text produces 768-dim vectors.
# This is separate from the main LLM model (config.OLLAMA_MODEL).
EMBEDDING_MODEL = "nomic-embed-text"

# Expected dimensionality. Used for validation. If you switch to a
# different embedding model, update this to match.
EMBEDDING_DIM = 768


def get_embedding(text: str) -> np.ndarray:
    """
    Generate a vector embedding for a text string.

    Calls the local Ollama embedding API. The embedding model runs
    separately from the main LLM, so this call is fast and doesn't
    block inference.

    Args:
        text: The text to embed. Can be a single message, a summary,
              or any other string.

    Returns:
        A numpy array of shape (EMBEDDING_DIM,) with dtype float32.

    Raises:
        RuntimeError: If the Ollama API call fails.
        ValueError: If the returned embedding has unexpected dimensions.
    """
    if not text.strip():
        raise ValueError("Cannot embed empty text.")

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/embed",
            json={
                "model": EMBEDDING_MODEL,
                "input": text,
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.ConnectionError:
        raise RuntimeError(
            f"Cannot connect to Ollama at {config.OLLAMA_BASE_URL}. "
            f"Is Ollama running? Start it with: ollama serve"
        )
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Ollama embedding API error: {e}. "
            f"Have you pulled the model? Run: ollama pull {EMBEDDING_MODEL}"
        )

    data = response.json()

    # The /api/embed endpoint returns {"embeddings": [[...], [...]]}
    # We send a single input, so we want the first (and only) embedding.
    embeddings = data.get("embeddings")
    if not embeddings or not embeddings[0]:
        raise RuntimeError(
            f"Ollama returned empty embeddings. Response: {data}"
        )

    vector = np.array(embeddings[0], dtype=np.float32)

    if vector.shape[0] != EMBEDDING_DIM:
        raise ValueError(
            f"Expected embedding dimension {EMBEDDING_DIM}, "
            f"got {vector.shape[0]}. Did the model change?"
        )

    return vector
