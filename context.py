"""
Context assembly — builds the full prompt for each LLM call.

This module is the single place where all context sources are combined
into the system prompt and messages array that brain.ask() receives.
Both main.py and telegram_bot.py call assemble_context() and pass the
result to brain.ask(). No other module builds prompts.

The system prompt is assembled from sections, each with a token budget:

    1. Persona prompt          (< 2,000 tokens)  — from personas/*.md
    2. User context            (< 2,000 tokens)  — from data/user_context.md
    3. Conversation summaries  (< 8,000 tokens)  — retrieved by semantic similarity
    4. Additional context      (< 8,000 tokens)  — calendar, email, etc. (future)

The messages array contains recent verbatim messages (< 32,000 tokens).

Summary retrieval works by embedding the user's most recent message and
finding stored summaries with the highest cosine similarity. Summaries
below a minimum similarity threshold are excluded to avoid injecting
irrelevant context. A few recent summaries are always included regardless
of similarity to maintain conversational continuity.

To add a new context source in the future:
    1. Write a function that returns a string (the content) or empty string
    2. Add it to the CONTEXT_SECTIONS list in _build_system_prompt()
    3. Give it a token budget
    That's it — the assembly loop handles truncation and formatting.
"""

import logging
from datetime import datetime
from pathlib import Path
from memory import PersonaMemory
from tokens import get_token_count

logger = logging.getLogger(__name__)

# --- Device Types ---
# Passed by the caller to control response length guidance.
DEVICE_TERMINAL = "terminal"
DEVICE_TELEGRAM = "telegram"

# --- Token Budgets ---
# These control how much space each section gets in the prompt.
# The total system prompt should stay under ~20,000 tokens to leave
# room for the messages array within the model's context window.

BUDGET_PERSONA = 2_000
BUDGET_USER_CONTEXT = 2_000
BUDGET_SUMMARIES = 8_000
BUDGET_ADDITIONAL = 8_000
BUDGET_MESSAGES = 32_000

# --- Summary Retrieval Settings ---

# Maximum number of summaries to retrieve per query.
SUMMARY_TOP_K = 8

# Minimum cosine similarity to include a summary. Summaries below
# this threshold are considered irrelevant noise. Based on observed
# scores: related text ~0.5-0.8, unrelated ~0.2-0.4.
# Start conservative; tune based on real data.
SUMMARY_MIN_SIMILARITY = 0.35

# Always include the N most recent summaries regardless of similarity.
# This maintains conversational continuity — the persona should know
# what was discussed recently even if the current message seems
# unrelated. Set to 0 to disable.
SUMMARY_ALWAYS_RECENT = 2

# --- File Paths ---

DATA_DIR = Path(__file__).parent / "data"
USER_CONTEXT_PATH = DATA_DIR / "user_context.md"


# --- Context Source Loaders ---
# Each loader returns a string. If the source is unavailable or empty,
# it returns an empty string. The assembly loop skips empty sections.

def _load_user_context() -> str:
    """
    Load the shared user context file.

    This file is manually maintained by the user and contains
    background information that all personas should know.
    Returns empty string if the file doesn't exist yet.
    """
    if not USER_CONTEXT_PATH.exists():
        return ""
    return USER_CONTEXT_PATH.read_text().strip()


def _load_session_context(device: str) -> str:
    """
    Generate context about the current session: time, date, and device.

    This gives the LLM awareness of when the conversation is happening
    and how to calibrate response length for the device.
    """
    now = datetime.now()
    timestamp = now.strftime("%A, %B %d, %Y at %I:%M %p")

    parts = [f"Current date and time: {timestamp}"]

    if device == DEVICE_TELEGRAM:
        parts.append(
            "The user is messaging from their phone via Telegram. "
            "Keep responses concise and scannable — short paragraphs, "
            "direct answers. Avoid long explanations unless asked."
        )
    elif device == DEVICE_TERMINAL:
        parts.append(
            "The user is at their computer using the terminal interface. "
            "Longer, more detailed responses are welcome when appropriate."
        )

    return "\n\n".join(parts)


def _load_summaries(memory: PersonaMemory, current_message: str) -> str:
    """
    Load relevant conversation summaries via semantic search.

    Embeds the user's most recent message and finds stored summaries
    with the highest cosine similarity. Also always includes the most
    recent summaries for continuity. Deduplicates and formats the
    results into a readable block for the system prompt.

    Args:
        memory: The persona's memory instance.
        current_message: The user's latest message text, used as the
            search query for semantic retrieval.

    Returns:
        Formatted string of relevant summaries, or empty string if
        no summaries exist or embedding is unavailable.
    """
    all_summaries = memory.get_all_summaries()
    if not all_summaries:
        return ""

    # Collect summaries from two sources: semantic search + recent

    # 1. Semantic search — find summaries similar to current message
    semantic_results = []
    if current_message.strip():
        try:
            from embeddings import get_embedding, EMBEDDING_DIM
            query_embedding = get_embedding(current_message)
            search_results = memory.search_summaries(
                query_embedding,
                top_k=SUMMARY_TOP_K,
                embedding_dim=EMBEDDING_DIM,
            )
            # Filter by minimum similarity
            semantic_results = [
                r for r in search_results
                if r["similarity"] >= SUMMARY_MIN_SIMILARITY
            ]
            for r in semantic_results:
                logger.debug(
                    f"Summary #{r['id']} similarity: {r['similarity']:.3f}"
                )
        except Exception as e:
            logger.warning(f"Summary embedding search failed: {e}")
            # Fall through to recent-only retrieval

    # 2. Recent summaries — always include for continuity
    recent_results = []
    if SUMMARY_ALWAYS_RECENT > 0:
        # all_summaries is ordered by message_start ASC, so take from the end
        recent_candidates = all_summaries[-SUMMARY_ALWAYS_RECENT:]
        for s in recent_candidates:
            recent_results.append({
                "id": s["id"],
                "summary": s["summary"],
                "created_at": s["created_at"],
                "similarity": None,  # Not from search
            })

    # 3. Merge and deduplicate (semantic results may overlap with recent)
    seen_ids = set()
    merged = []

    # Semantic results first (they're ranked by relevance)
    for r in semantic_results:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            merged.append(r)

    # Then recent results (for continuity)
    for r in recent_results:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            merged.append(r)

    if not merged:
        return ""

    # 4. Format for the system prompt
    parts = []
    for r in merged:
        sim_note = f" (relevance: {r['similarity']:.2f})" if r["similarity"] is not None else ""
        date = r.get("created_at", "unknown date")
        parts.append(f"[{date}]{sim_note}\n{r['summary']}")

    return "\n\n---\n\n".join(parts)


def _load_additional_context() -> str:
    """
    Load additional context from external sources.

    Placeholder for future integrations:
        - Google Calendar (upcoming events, today's schedule)
        - Gmail (recent important emails)
        - Other tools

    Each integration will be its own module that returns a
    formatted string. This function will call them and combine
    the results.
    """
    # TODO: Implement external integrations (Stages 5+)
    return ""


# --- Truncation ---

def _truncate_to_budget(text: str, budget: int) -> str:
    """
    Truncate text to fit within a token budget.

    If the text is within budget, returns it unchanged. If it exceeds
    the budget, truncates to approximately the right length and adds
    a note that content was truncated.

    This is a safety net, not the primary control mechanism. If a
    section is regularly getting truncated, its source should be
    shortened or its budget increased.
    """
    if not text:
        return text

    token_count = get_token_count(text)
    if token_count <= budget:
        return text

    # Approximate character limit (4 chars per token)
    char_limit = budget * 4
    truncated = text[:char_limit].rsplit(" ", 1)[0]  # Break at word boundary
    return truncated + "\n\n[... content truncated to fit token budget ...]"


# --- Assembly ---

def _build_system_prompt(
    persona_prompt: str,
    memory: PersonaMemory,
    current_message: str,
    device: str,
) -> str:
    """
    Assemble the full system prompt from all context sources.

    Each section is loaded, truncated to its budget, and combined
    with clear separators so the LLM can distinguish between them.
    Empty sections are skipped entirely.
    """
    # Define sections: (label, content, budget)
    # The label helps the LLM understand what each block is.
    # Order matters — persona identity comes first, then user context,
    # then session info, then memory, then supplementary info.
    sections = [
        ("PERSONA", persona_prompt, BUDGET_PERSONA),
        ("ABOUT THE USER", _load_user_context(), BUDGET_USER_CONTEXT),
        ("CURRENT SESSION", _load_session_context(device), 500),
        ("RELEVANT PAST CONVERSATIONS", _load_summaries(memory, current_message), BUDGET_SUMMARIES),
        ("ADDITIONAL CONTEXT", _load_additional_context(), BUDGET_ADDITIONAL),
    ]

    parts = []
    for label, content, budget in sections:
        if not content:
            continue
        truncated = _truncate_to_budget(content, budget)
        parts.append(f"## {label}\n\n{truncated}")

    return "\n\n---\n\n".join(parts)


def _build_messages(memory: PersonaMemory, max_tokens: int = BUDGET_MESSAGES) -> list[dict]:
    """
    Load recent messages from the database, fitting within the token budget.

    Starts from the most recent messages and works backward until the
    budget is exhausted. Returns messages in chronological order (oldest
    first), which is what the LLM expects.

    User messages include a timestamp prefix so the LLM knows when
    each message was sent. Assistant messages are left clean.
    """
    # Load a generous batch — more than we'll likely need.
    # We'll trim by token count below.
    all_recent = memory.get_recent_messages(limit=200)

    if not all_recent:
        return []

    # Work backward from the most recent message, accumulating
    # until we hit the token budget.
    selected = []
    running_tokens = 0

    for msg in reversed(all_recent):
        # Add timestamp to user messages
        if msg["role"] == "user" and msg.get("created_at"):
            content = f"[{msg['created_at']}] {msg['content']}"
        else:
            content = msg["content"]

        msg_tokens = get_token_count(content)
        if running_tokens + msg_tokens > max_tokens:
            break
        selected.append({"role": msg["role"], "content": content})
        running_tokens += msg_tokens

    # Reverse back to chronological order
    selected.reverse()
    return selected


def assemble_context(
    persona_prompt: str,
    memory: PersonaMemory,
    device: str = DEVICE_TERMINAL,
) -> tuple[str, list[dict]]:
    """
    Build the complete context for an LLM call.

    This is the function that main.py and telegram_bot.py call.
    It returns everything brain.ask() needs: a system prompt string
    and a messages list.

    The most recent user message is used as the query for semantic
    summary retrieval — this determines which past conversation
    summaries are relevant to the current discussion.

    Args:
        persona_prompt: The persona's system prompt (from personas/*.md).
        memory: The persona's memory instance.
        device: The interface being used (DEVICE_TERMINAL or DEVICE_TELEGRAM).
            Controls response length guidance in the system prompt.

    Returns:
        A tuple of (system_prompt, messages) ready to pass to brain.ask().

    Example:
        system, messages = assemble_context(persona_prompt, memory, device="telegram")
        response = brain.ask(messages, system=system, provider=provider)
    """
    # Build the messages array first — we need the most recent user
    # message to drive summary retrieval.
    messages = _build_messages(memory)

    # Extract the most recent user message for the retrieval query.
    # Search backward through messages to find the last user turn.
    current_message = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            current_message = msg["content"]
            break

    system_prompt = _build_system_prompt(persona_prompt, memory, current_message, device)
    return system_prompt, messages