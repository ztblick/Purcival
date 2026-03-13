"""
Token counting utilities.

Provides a single function for estimating token counts. The rest of the
app calls get_token_count() and doesn't care how the estimate is produced.

Current implementation: simple character-based approximation (~4 chars per
token for English text). This is good enough for budget enforcement where
we have generous margins. If precision matters later, swap in a real
tokenizer (like tiktoken) — no other code needs to change.
"""


def get_token_count(text: str) -> int:
    """
    Estimate the number of tokens in a string.

    Args:
        text: The text to measure.

    Returns:
        Estimated token count.
    """
    # ~4 characters per token is a reasonable approximation for English.
    # This tends to slightly overestimate, which is the safe direction
    # for budget enforcement (better to leave room than to overflow).
    return len(text) // 4
