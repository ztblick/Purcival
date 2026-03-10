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
# Persona files live in the personas/ directory
DEFAULT_PERSONA = os.getenv("DEFAULT_PERSONA", "default")
