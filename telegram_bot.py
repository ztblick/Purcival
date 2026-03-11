"""
Telegram bot — the mobile interface for Purcival.

Each instance of this bot serves exactly ONE persona. To run multiple
personas, you launch multiple processes:

    python run_telegram.py --persona percival
    python run_telegram.py --persona jocelyn

Each persona has its own Telegram bot (its own @username, avatar, and
chat thread on your phone) and its own bot token in .env.

How it works:
    1. The bot long-polls Telegram's servers (no inbound ports needed)
    2. When a message arrives, it checks if the sender is authorized
    3. It appends the message to the in-memory conversation history
    4. It calls brain.ask() with this persona's system prompt
    5. It sends the response back through Telegram

Security:
    Only your Telegram user ID can talk to the bot. Everyone else
    gets silently ignored. Set TELEGRAM_ALLOWED_USER_ID in .env.
"""

import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ChatAction

import brain
import config
import personas

logger = logging.getLogger(__name__)


class PersonaBot:
    """
    A Telegram bot bound to a single persona.

    Each instance manages its own conversation history, system prompt,
    and Telegram bot token. Multiple instances can run concurrently
    as separate processes.
    """

    def __init__(self, persona_name: str):
        self.persona_name = persona_name
        self.system_prompt = personas.load_persona(persona_name)
        self.token = config.get_telegram_token(persona_name)
        self.provider = config.DEFAULT_PROVIDER

        # In-memory conversation history. Lost on restart.
        # Keyed by user ID in case multiple users are authorized later.
        self._history: dict[int, list[dict]] = {}

    def _get_history(self, user_id: int) -> list[dict]:
        """Get or create conversation history for a user."""
        if user_id not in self._history:
            self._history[user_id] = []
        return self._history[user_id]

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is allowed to talk to this bot."""
        if not config.TELEGRAM_ALLOWED_USER_ID:
            return True
        return str(user_id) == str(config.TELEGRAM_ALLOWED_USER_ID)

    # --- Command Handlers ---

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start."""
        if not self._is_authorized(update.effective_user.id):
            return

        await update.message.reply_text(
            f"Hey! I'm *{self.persona_name.title()}*, your Purcival assistant.\n\n"
            f"Just send me a message and I'll respond.\n\n"
            f"Commands:\n"
            f"/provider — switch between claude and ollama\n"
            f"/status — show current state\n"
            f"/clear — reset conversation history",
            parse_mode="Markdown",
        )

    async def cmd_provider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /provider — switch LLM backend."""
        if not self._is_authorized(update.effective_user.id):
            return

        if context.args and context.args[0].lower() in ("claude", "ollama"):
            self.provider = context.args[0].lower()
            await update.message.reply_text(
                f"Switched to *{self.provider}*.",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            f"Current provider: *{self.provider}*\n"
            f"Usage: `/provider claude` or `/provider ollama`",
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status."""
        if not self._is_authorized(update.effective_user.id):
            return

        user_id = update.effective_user.id
        history = self._get_history(user_id)
        model = (config.CLAUDE_MODEL if self.provider == "claude"
                 else config.OLLAMA_MODEL)

        await update.message.reply_text(
            f"*Persona:* {self.persona_name}\n"
            f"*Provider:* {self.provider}\n"
            f"*Model:* `{model}`\n"
            f"*History:* {len(history)} messages",
            parse_mode="Markdown",
        )

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /clear."""
        if not self._is_authorized(update.effective_user.id):
            return

        user_id = update.effective_user.id
        self._history[user_id] = []
        await update.message.reply_text("Conversation cleared.")

    # --- Message Handler ---

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle a regular text message — the core conversation loop."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            logger.warning(f"Unauthorized message from user {user_id}")
            return

        history = self._get_history(user_id)
        user_text = update.message.text

        # Add user message to history
        history.append({"role": "user", "content": user_text})

        # Show "typing..." indicator while the model thinks
        await update.message.chat.send_action(ChatAction.TYPING)

        # Get the response
        try:
            response = brain.ask(
                history,
                system=self.system_prompt,
                provider=self.provider,
            )
        except Exception as e:
            logger.error(f"Brain error: {e}")
            history.pop()
            await update.message.reply_text(f"Error: {e}")
            return

        # Add response to history
        history.append({"role": "assistant", "content": response})

        # Send back — split if over Telegram's 4096 char limit
        if len(response) <= 4096:
            # Try markdown first, fall back to plain text if it fails
            try:
                await update.message.reply_text(response, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(response)
        else:
            for i in range(0, len(response), 4096):
                await update.message.reply_text(response[i:i + 4096])

    # --- Entry Point ---

    def run(self):
        """Start the bot with long polling. Blocks forever."""
        app = Application.builder().token(self.token).build()

        # Register handlers — we wrap methods so the library can call them
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("provider", self.cmd_provider))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("clear", self.cmd_clear))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        logger.info(f"Starting Telegram bot for persona: {self.persona_name}")
        logger.info(f"Default provider: {self.provider}")

        app.run_polling(
            poll_interval=0,
            timeout=30,
            drop_pending_updates=True,
        )
