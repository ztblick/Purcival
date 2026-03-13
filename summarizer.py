"""
Summarizer — compresses older conversations into searchable summaries.

This module handles the full summarization lifecycle:
    1. Check if unsummarized messages exceed the token threshold
    2. Take the oldest batch of unsummarized messages
    3. Send them to the local LLM with a summarization prompt
    4. Embed the resulting summary for semantic search
    5. Store the summary and embedding in the persona's database

Summarization runs AFTER the user-facing response has been sent,
so it never adds latency to the conversation. It always uses the
local LLM (via Ollama), never Claude, to avoid burning API credits
on a background task.

Usage:
    from summarizer import check_and_summarize
    # Call after every response:
    check_and_summarize(memory)
"""

import logging
import brain
from memory import PersonaMemory
from embeddings import get_embedding
from tokens import get_token_count

logger = logging.getLogger(__name__)

# --- Configuration ---

# Summarize when unsummarized messages exceed this many tokens.
# This should be well below the context window to trigger before
# old messages would be pushed out of the verbatim window.
SUMMARIZE_THRESHOLD_TOKENS = 24_000

# How many tokens of messages to include in each summary batch.
# We take roughly half the threshold, leaving the other half as
# recent messages that stay verbatim in the next prompt.
SUMMARIZE_BATCH_TOKENS = 12_000

# Always use local inference for summarization — never burn API
# credits on a background task.
SUMMARIZE_PROVIDER = "ollama"

# --- Summarization Prompt ---

SUMMARIZE_SYSTEM_PROMPT = """\
You are a memory system for a personal AI assistant called Purcival. Your task \
is to read a conversation and write a concise summary that captures what a \
friend would remember after the conversation.

Focus on specifics: names, dates, numbers, decisions, plans, and concrete \
details. These are far more valuable than general impressions. If the user \
mentioned a person, note who they are. If a decision was made, note what was \
decided. If an event or deadline was referenced, note it.

The conversation includes timestamps. Always note when the conversation took \
place (e.g. "On the evening of March 12" or "Wednesday afternoon"). This \
helps distinguish between recent and older memories.

Only record what was actually said. If something was not discussed, do not \
mention it. Never invent or infer details to fill gaps. A shorter summary \
that is accurate is better than a longer one that guesses.

Write the summary as a natural paragraph or two. Do not use category headers, \
bullet points, or numbered lists. Do not include any preamble like "Here is \
a summary" — just write the summary directly.\
"""


def _format_messages_for_summary(messages: list[dict]) -> str:
    """
    Format a batch of messages into a readable transcript for the LLM.

    Uses neutral labels rather than "User"/"Assistant" to reduce the
    chance of the model falling into conversational response mode
    instead of summarizing. Includes timestamps so the summary can
    reference when events occurred.
    """
    lines = []
    for msg in messages:
        label = "Zach" if msg["role"] == "user" else "AI"
        timestamp = msg.get("created_at", "")
        if timestamp:
            lines.append(f"[{label}, {timestamp}]: {msg['content']}")
        else:
            lines.append(f"[{label}]: {msg['content']}")
    return "\n\n".join(lines)


def _select_batch(messages: list[dict], max_tokens: int) -> list[dict]:
    """
    Select a batch of messages from the front (oldest) of the list,
    up to the token budget.

    Returns the subset of messages that fit. Messages are not split —
    we take whole messages until adding the next one would exceed
    the budget.
    """
    batch = []
    running_tokens = 0

    for msg in messages:
        msg_tokens = get_token_count(msg["content"])
        if running_tokens + msg_tokens > max_tokens and batch:
            # Budget exceeded and we have at least some messages
            break
        batch.append(msg)
        running_tokens += msg_tokens

    return batch


def _generate_summary(messages: list[dict]) -> str:
    """
    Send a batch of messages to the local LLM for summarization.

    Uses brain.ask() with the summarization system prompt and
    always routes to the local provider.
    """
    transcript = _format_messages_for_summary(messages)

    user_prompt = (
        "Summarize the following conversation:\n\n"
        f"{transcript}"
    )

    summary = brain.ask(
        messages=[{"role": "user", "content": user_prompt}],
        system=SUMMARIZE_SYSTEM_PROMPT,
        provider=SUMMARIZE_PROVIDER,
    )

    return summary.strip()


def check_and_summarize(memory: PersonaMemory) -> bool:
    """
    Check if summarization is needed and run it if so.

    This is the function that telegram_bot.py and main.py call
    after every response. It checks the unsummarized message count,
    and if it exceeds the threshold, generates a summary of the
    oldest batch.

    Args:
        memory: The persona's memory instance.

    Returns:
        True if a summary was generated, False if not needed.
    """
    unsummarized = memory.get_unsummarized_messages()

    # Count tokens in unsummarized messages
    total_tokens = sum(get_token_count(m["content"]) for m in unsummarized)

    if total_tokens < SUMMARIZE_THRESHOLD_TOKENS:
        return False

    logger.info(
        f"Summarization triggered: {total_tokens} tokens unsummarized "
        f"(threshold: {SUMMARIZE_THRESHOLD_TOKENS})"
    )

    # Select the oldest batch to summarize
    batch = _select_batch(unsummarized, SUMMARIZE_BATCH_TOKENS)

    if len(batch) < 2:
        # Need at least one exchange to make a meaningful summary
        logger.warning("Batch too small to summarize, skipping.")
        return False

    # Record the message ID range this summary will cover
    message_start = batch[0]["id"]
    message_end = batch[-1]["id"]

    # Generate the summary
    try:
        logger.info(
            f"Summarizing messages {message_start}-{message_end} "
            f"({len(batch)} messages)"
        )
        summary_text = _generate_summary(batch)
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return False

    # Embed the summary for semantic search
    try:
        embedding = get_embedding(summary_text)
    except Exception as e:
        logger.error(f"Summary embedding failed: {e}")
        # Store the summary without an embedding — it won't be
        # searchable, but it's better than losing it entirely.
        memory.add_summary(
            summary=summary_text,
            message_start=message_start,
            message_end=message_end,
            embedding=None,
        )
        logger.info("Summary stored without embedding.")
        return True

    # Store summary with embedding
    summary_id = memory.add_summary(
        summary=summary_text,
        message_start=message_start,
        message_end=message_end,
        embedding=embedding,
    )

    logger.info(
        f"Summary #{summary_id} stored: covers messages "
        f"{message_start}-{message_end}, "
        f"{get_token_count(summary_text)} tokens"
    )

    return True