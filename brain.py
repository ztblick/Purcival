"""
The Brain — handles all LLM communication.

This module abstracts the language model behind a simple interface:
    send a list of messages in, get a response back.

Supports three providers:
    - "claude"   → Anthropic API (cloud, highest quality, costs money)
    - "chatgpt"  → OpenAI API (cloud, alternative cloud option)
    - "ollama"   → Local inference via Ollama (free, private, runs on your GPU)

The active provider is set by DEFAULT_PROVIDER in config. All three call sites
(chat, summary, reasoning) use the same provider family; the `task` parameter
selects the right per-task model within that family.

If the configured provider is unavailable (missing API key), brain.ask() falls
back to Ollama automatically with a warning.

Architecture note:
    All APIs are stateless. Neither remembers previous messages.
    Every call sends the full conversation history.
    The "memory" lives in our code, not in the model.
"""

import logging
import requests
import config

logger = logging.getLogger(__name__)


# --- Claude Provider ---

_claude_client = None


def _ensure_claude():
    """Lazy-load the Claude client. Raises RuntimeError if no key is set."""
    global _claude_client
    if _claude_client is not None:
        return _claude_client
    if not config.ANTHROPIC_API_KEY or config.ANTHROPIC_API_KEY == "your-key-here":
        raise RuntimeError("Claude provider requires ANTHROPIC_API_KEY in .env")
    from anthropic import Anthropic
    _claude_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _claude_client


def _ask_claude(messages: list[dict], system: str, max_tokens: int, model: str) -> str:
    """Send messages to Claude via the Anthropic API."""
    client = _ensure_claude()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return response.content[0].text


# --- ChatGPT Provider ---

_chatgpt_client = None


def _ensure_chatgpt():
    """Lazy-load the ChatGPT client. Raises RuntimeError if no key is set."""
    global _chatgpt_client
    if _chatgpt_client is not None:
        return _chatgpt_client
    if not config.OPENAI_API_KEY:
        raise RuntimeError("ChatGPT provider requires OPENAI_API_KEY in .env")
    from openai import OpenAI
    _chatgpt_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _chatgpt_client


def _ask_chatgpt(messages: list[dict], system: str, max_tokens: int, model: str) -> str:
    """
    Send messages to ChatGPT via the OpenAI Chat Completions API.

    System prompt is prepended as a "system" role message — the same pattern
    used by Ollama, since both implement the OpenAI message format.
    """
    client = _ensure_chatgpt()
    full_messages = [{"role": "system", "content": system}] + messages
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        messages=full_messages,
    )
    return response.choices[0].message.content


# --- Ollama Provider ---

def _ask_ollama(messages: list[dict], system: str, max_tokens: int, model: str) -> str:
    """
    Send messages to a local model via Ollama's OpenAI-compatible API.

    Ollama runs on your machine and exposes an API at localhost:11434.
    The system prompt gets prepended as a "system" role message —
    the OpenAI-compatible format for system instructions.
    """
    full_messages = [{"role": "system", "content": system}] + messages

    response = requests.post(
        f"{config.OLLAMA_BASE_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": full_messages,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    response.raise_for_status()

    data = response.json()
    return data["choices"][0]["message"]["content"]


# --- Dispatch Tables ---

# Maps (provider, task) → callable returning the configured model string.
# Lambdas read from config at call time so .env overrides take effect without
# restarting Python.
_MODELS: dict[tuple[str, str], object] = {
    ("claude",   "chat"):      lambda: config.CLAUDE_CHAT_MODEL,
    ("claude",   "summary"):   lambda: config.CLAUDE_SUMMARY_MODEL,
    ("claude",   "reasoning"): lambda: config.CLAUDE_REASONING_MODEL,
    ("chatgpt",  "chat"):      lambda: config.CHATGPT_CHAT_MODEL,
    ("chatgpt",  "summary"):   lambda: config.CHATGPT_SUMMARY_MODEL,
    ("chatgpt",  "reasoning"): lambda: config.CHATGPT_REASONING_MODEL,
    ("ollama",   "chat"):      lambda: config.OLLAMA_CHAT_MODEL,
    ("ollama",   "summary"):   lambda: config.OLLAMA_SUMMARY_MODEL,
    ("ollama",   "reasoning"): lambda: config.OLLAMA_REASONING_MODEL,
}

# Maps provider name → handler function.
_HANDLERS = {
    "claude":  _ask_claude,
    "chatgpt": _ask_chatgpt,
    "ollama":  _ask_ollama,
}


def _resolve_model(provider: str, task: str) -> str:
    """Look up the model string for a given provider and task."""
    getter = _MODELS.get((provider, task)) or _MODELS.get((provider, "chat"))
    if getter is None:
        return config.OLLAMA_CHAT_MODEL
    return getter()


# --- Public Interface ---

def ask(
    messages: list[dict],
    system: str | None = None,
    provider: str | None = None,
    max_tokens: int = 2048,
    task: str = "chat",
) -> str:
    """
    Send a conversation to an LLM and get a response.

    Args:
        messages:   List of message dicts with 'role' and 'content'.
        system:     The system prompt (persona). Required.
        provider:   "claude", "chatgpt", or "ollama". Defaults to
                    config.DEFAULT_PROVIDER. Pass explicitly to override
                    (e.g. for runtime provider switching in the CLI).
        max_tokens: Maximum tokens in the response. Default 2048;
                    use 4096 for agent reasoning calls.
        task:       Which call site this serves: "chat", "summary", or
                    "reasoning". Selects the per-task model within the
                    provider family. Defaults to "chat".

    Returns:
        The assistant's response as a string.

    Fallback:
        If the requested provider is unknown or misconfigured (missing
        API key), the call falls back to Ollama and logs a warning.
    """
    if system is None:
        raise ValueError("system prompt is required — load a persona first")

    effective = provider or config.DEFAULT_PROVIDER
    handler = _HANDLERS.get(effective)

    if handler is None:
        logger.warning(f"Unknown provider '{effective}', falling back to ollama")
        effective = "ollama"
        handler = _ask_ollama

    model = _resolve_model(effective, task)

    try:
        return handler(messages, system, max_tokens, model)
    except RuntimeError as e:
        if effective != "ollama":
            logger.warning(
                f"Provider '{effective}' unavailable: {e}. Falling back to ollama."
            )
            fallback_model = _resolve_model("ollama", task)
            return _ask_ollama(messages, system, max_tokens, fallback_model)
        raise
