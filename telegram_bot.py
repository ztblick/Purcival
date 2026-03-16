"""
Telegram bot — the mobile interface for Purcival.

Each instance of this bot serves exactly ONE persona. To run multiple
personas, you launch multiple processes:

    python run_telegram.py --persona purcival
    python run_telegram.py --persona jocelyn

Each persona has its own Telegram bot (its own @username, avatar, and
chat thread on your phone) and its own bot token in .env.

How it works:
    1. The bot long-polls Telegram's servers (no inbound ports needed)
    2. When a message arrives, it checks if the sender is authorized
    3. It persists the message to the persona's SQLite database
    4. It loads recent conversation history from the database
    5. It calls brain.ask() with this persona's system prompt
    6. It persists the response and sends it back through Telegram

Security:
    Only your Telegram user ID can talk to the bot. Everyone else
    gets silently ignored. Set TELEGRAM_ALLOWED_USER_ID in .env.

Memory:
    All messages are stored in data/<persona>/memory.db. Conversations
    survive restarts, reboots, and crashes. The /clear command wipes
    the database for this persona only.
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
from memory import PersonaMemory
from context import assemble_context, DEVICE_TELEGRAM
from summarizer import check_and_summarize
from proactive import start_scheduler, seed_hourly_triggers

logger = logging.getLogger(__name__)


class PersonaBot:
    """
    A Telegram bot bound to a single persona.

    Each instance manages its own persistent memory, system prompt,
    and Telegram bot token. Multiple instances can run concurrently
    as separate processes, each with its own database.
    """

    def __init__(self, persona_name: str):
        self.persona_name = persona_name
        self.persona_prompt = personas.load_persona(persona_name)
        self.token = config.get_telegram_token(persona_name)
        self.provider = config.DEFAULT_PROVIDER

        # Persistent memory
        self.memory = PersonaMemory(persona_name)

        # Telegram chat ID for proactive messaging. Set on first
        # authorized message received — we can't send proactive
        # messages until the user has messaged us at least once.
        self._chat_id: int | None = None
        self._app = None  # Set in run(), needed for proactive sends

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
            f"/status — show current state",
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

        model = (config.CLAUDE_MODEL if self.provider == "claude"
                 else config.OLLAMA_MODEL)
        total_messages = self.memory.get_message_count()
        total_summaries = len(self.memory.get_all_summaries())

        await update.message.reply_text(
            f"*Persona:* {self.persona_name}\n"
            f"*Provider:* {self.provider}\n"
            f"*Model:* `{model}`\n"
            f"*Messages stored:* {total_messages}\n"
            f"*Summaries:* {total_summaries}",
            parse_mode="Markdown",
        )

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /clear — disabled on Telegram to prevent accidental data loss."""
        if not self._is_authorized(update.effective_user.id):
            return

        await update.message.reply_text(
            "Memory clearing is disabled on Telegram to prevent accidental "
            "data loss. Use the terminal interface (main.py) to clear history."
        )

    # --- Message Handler ---

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle a regular text message — the core conversation loop."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            logger.warning(f"Unauthorized message from user {user_id}")
            return

        # Capture chat ID for proactive messaging
        if self._chat_id is None:
            self._chat_id = update.effective_chat.id
            logger.info(f"Chat ID captured: {self._chat_id}")

        user_text = update.message.text

        # Persist the user's message FIRST — even if the LLM call fails,
        # we have a record of what was said.
        self.memory.add_message("user", user_text)

        # Assemble the full context: system prompt + recent messages
        system_prompt, messages = assemble_context(
            self.persona_prompt, self.memory, device=DEVICE_TELEGRAM
        )

        # Show "typing..." indicator while the model thinks
        await update.message.chat.send_action(ChatAction.TYPING)

        # Get the response
        try:
            response = brain.ask(
                messages,
                system=system_prompt,
                provider=self.provider,
            )
        except Exception as e:
            logger.error(f"Brain error: {e}")
            await update.message.reply_text(f"Error: {e}")
            return

        # Persist the assistant's response
        self.memory.add_message("assistant", response)

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

        # Check if older messages need summarization.
        # This runs AFTER the response is sent, so the user never waits.
        try:
            check_and_summarize(self.memory)
        except Exception as e:
            logger.error(f"Summarization error: {e}")

    # --- Entry Point ---

    def run(self):
        """Start the bot with long polling. Blocks forever."""
        app = Application.builder().token(self.token).build()
        self._app = app

        # Register handlers
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("provider", self.cmd_provider))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("clear", self.cmd_clear))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        # Seed recurring triggers (this is synchronous, fine to do now)
        seed_hourly_triggers(self.memory)

        async def send_proactive(text: str):
            """Send a proactive message to the user via Telegram."""
            if self._chat_id is None:
                logger.warning(
                    "Cannot send proactive message: no chat ID yet. "
                    "User must send at least one message first."
                )
                return
            try:
                await app.bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode="Markdown",
                )
            except Exception:
                # Fallback to plain text if markdown fails
                await app.bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                )

        # Start the scheduler AFTER the event loop is running.
        # app.post_init runs after the Application has started its
        # asyncio event loop, which APScheduler's AsyncIOScheduler needs.
        async def on_startup(application):
            start_scheduler(self.memory, send_proactive, self.persona_prompt)

        app.post_init = on_startup

        logger.info(f"Starting Telegram bot for persona: {self.persona_name}")
        logger.info(f"Default provider: {self.provider}")
        logger.info(f"Memory: {self.memory.db_path}")

        app.run_polling(
            poll_interval=0,
            timeout=30,
            drop_pending_updates=True,
        )