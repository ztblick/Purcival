"""
The Brain — handles all LLM communication.

This module abstracts the language model behind a simple interface:
    send a list of messages in, get a response back.

Supports two providers:
    - "claude"  → Anthropic API (cloud, highest quality, costs money)
    - "ollama"  → Local inference via Ollama (free, private, runs on your GPU)

Both providers receive the same message format and system prompt.
The rest of the app doesn't know or care which one is responding.

Architecture note:
    Both APIs are stateless. Neither remembers previous messages.
    Every time we call either one, we send the FULL conversation history.
    The "memory" lives in our code, not in the model.
"""

import requests
import config

# --- Claude Provider ---

# Only initialize the Anthropic client if we have a key
_claude_client = None

def _ensure_claude():
    """Lazy-load the Claude client. Fails helpfully if no key is set."""
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    if not config.ANTHROPIC_API_KEY or config.ANTHROPIC_API_KEY == "your-key-here":
        raise RuntimeError(
            "Claude provider requires ANTHROPIC_API_KEY in .env"
        )
    from anthropic import Anthropic
    _claude_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _claude_client


def _ask_claude(messages: list[dict], system: str) -> str:
    """Send messages to Claude via the Anthropic API."""
    client = _ensure_claude()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2048,
        system=system,
        messages=messages,
    )
    return response.content[0].text


# --- Ollama Provider ---

def _ask_ollama(messages: list[dict], system: str) -> str:
    """
    Send messages to a local model via Ollama's OpenAI-compatible API.

    Ollama runs on your machine and exposes an API at localhost:11434.
    It uses the same message format as OpenAI/Claude, which is why
    our conversation history works with both providers unchanged.

    The system prompt gets prepended as a "system" role message —
    this is how OpenAI-compatible APIs handle it, slightly different
    from Anthropic's dedicated system parameter.
    """
    # Build the message list with system prompt first
    full_messages = [{"role": "system", "content": system}] + messages

    response = requests.post(
        f"{config.OLLAMA_BASE_URL}/v1/chat/completions",
        json={
            "model": config.OLLAMA_MODEL,
            "messages": full_messages,
        },
        timeout=120,  # Local models can be slow on first response
    )
    response.raise_for_status()

    data = response.json()
    return data["choices"][0]["message"]["content"]


# --- Router ---

# Map provider names to their handler functions
_PROVIDERS = {
    "claude": _ask_claude,
    "ollama": _ask_ollama,
}


def ask(
    messages: list[dict],
    system: str | None = None,
    provider: str | None = None,
) -> str:
    """
    Send a conversation to an LLM and get a response.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        system:   The system prompt (persona). Required — passed in by the caller.
        provider: "claude" or "ollama". Defaults to config.DEFAULT_PROVIDER.

    Returns:
        The assistant's response as a string.

    This is the only function the rest of the app calls to "think."
    Main, Telegram, the scheduler — they all use this one function.
    They don't know which model is answering.
    """
    if system is None:
        raise ValueError("system prompt is required — load a persona first")
    if provider is None:
        provider = config.DEFAULT_PROVIDER

    handler = _PROVIDERS.get(provider)
    if handler is None:
        raise ValueError(
            f"Unknown provider '{provider}'. Options: {list(_PROVIDERS.keys())}"
        )

    return handler(messages, system)
