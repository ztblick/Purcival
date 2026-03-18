"""
Configuration loader.

Reads settings from .env file and environment variables.
This is the single source of truth for all config — no other file
should read environment variables directly.
"""

import os
from dotenv import load_dotenv

# Load .env file from project root
load_dotenv()


# --- Provider Configuration ---
# Which LLM provider to use by default: "claude" or "ollama"
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "ollama")

# --- Claude (Anthropic API) ---
# Optional for now — set to empty string if not configured
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Ollama (local inference) ---
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi4")

# --- Personas ---
# Which persona to load by default (filename without .md extension)
DEFAULT_PERSONA = os.getenv("DEFAULT_PERSONA", "default")

# --- Telegram ---
# Your Telegram user ID — only this user can talk to the bots.
# Message @userinfobot on Telegram to find yours.
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")

# Your Telegram chat ID — required for proactive messaging.
# Set this once and proactive messages work immediately on restart,
# no need to send the bot a message first.
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def get_telegram_token(persona_name: str) -> str:
    """
    Get the Telegram bot token for a specific persona.

    Each persona has its own bot and its own token.
    Tokens are stored as environment variables:
        TELEGRAM_TOKEN_PURCIVAL=123456:ABC...

    This keeps tokens out of persona files (which are system prompts,
    not config) and lets you manage all secrets in one place (.env).
    """
    key = f"TELEGRAM_TOKEN_{persona_name.upper()}"
    token = os.getenv(key, "")
    if not token:
        raise RuntimeError(
            f"No Telegram token for persona '{persona_name}'.\n"
            f"Set {key} in your .env file.\n"
            f"Get a token from @BotFather on Telegram."
        )
    return token