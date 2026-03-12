"""
Launch a Telegram bot for a specific persona.

Usage:
    python run_telegram.py --persona purcival
    python run_telegram.py --persona jocelyn

Each persona runs as its own process with its own Telegram bot.
To run multiple personas simultaneously, open multiple terminals
(or use multiple systemd services).

This script is designed to run forever — either in a terminal,
in tmux, or as a systemd service.
"""

import argparse
import logging

import config
import personas
from telegram_bot import PersonaBot

# Configure logging for long-running service
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
# Quiet down the noisy HTTP libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("purcival")


def main():
    parser = argparse.ArgumentParser(
        description="Run a Purcival Telegram bot for a specific persona"
    )
    parser.add_argument(
        "--persona",
        type=str,
        required=True,
        help="Which persona to run (e.g. purcival, jocelyn, default)",
    )
    args = parser.parse_args()

    name = args.persona.lower()

    # Validate persona exists
    if not personas.persona_exists(name):
        available = personas.list_personas()
        logger.error(f"Persona '{name}' not found. Available: {', '.join(available)}")
        raise SystemExit(1)

    # Validate token is configured
    try:
        config.get_telegram_token(name)
    except RuntimeError as e:
        logger.error(str(e))
        raise SystemExit(1)

    # Launch the bot
    logger.info(f"Starting Purcival as '{name}'")
    bot = PersonaBot(name)
    bot.run()


if __name__ == "__main__":
    main()