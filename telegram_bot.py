"""
Telegram bot — the mobile interface for Purcival.

Each instance of this bot serves exactly ONE persona. To run multiple
personas, you launch multiple processes:

    python run_telegram.py --persona purcival
    python run_telegram.py --persona jocelyn

Each persona has its own Telegram bot (its own @username, avatar, and
chat thread on your phone) and its own bot token in .env.

Security:
    Only your Telegram user ID can talk to the bot. Everyone else
    gets silently ignored. Set TELEGRAM_ALLOWED_USER_ID in .env.
"""

import json
import logging
import re
from datetime import datetime

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
from proactive import start_scheduler, ensure_agent_has_plan
from agent import strip_schedule_updates, apply_schedule_updates

logger = logging.getLogger(__name__)


class PersonaBot:
    """
    A Telegram bot bound to a single persona.
    """

    def __init__(self, persona_name: str):
        self.persona_name = persona_name
        self.persona_prompt = personas.load_persona(persona_name)
        self.token = config.get_telegram_token(persona_name)
        self.provider = config.DEFAULT_PROVIDER

        self.memory = PersonaMemory(persona_name)

        if config.TELEGRAM_CHAT_ID:
            self._chat_id: int | None = int(config.TELEGRAM_CHAT_ID)
        else:
            saved_chat_id = self.memory.get_tool_state("telegram", "chat_id")
            self._chat_id: int | None = int(saved_chat_id) if saved_chat_id else None
        self._app = None

    def _is_authorized(self, user_id: int) -> bool:
        if not config.TELEGRAM_ALLOWED_USER_ID:
            return True
        return str(user_id) == str(config.TELEGRAM_ALLOWED_USER_ID)

    # --- Command Handlers ---

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        if not self._is_authorized(update.effective_user.id):
            return
        if context.args and context.args[0].lower() in ("claude", "ollama"):
            self.provider = context.args[0].lower()
            await update.message.reply_text(
                f"Switched to *{self.provider}*.", parse_mode="Markdown",
            )
            return
        await update.message.reply_text(
            f"Current provider: *{self.provider}*\n"
            f"Usage: `/provider claude` or `/provider ollama`",
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update.effective_user.id):
            return

        model = (config.CLAUDE_MODEL if self.provider == "claude"
                 else config.OLLAMA_MODEL)
        total_messages = self.memory.get_message_count()
        total_summaries = len(self.memory.get_all_summaries())
        active_triggers = self.memory.get_active_triggers()
        schedule = self.memory.get_schedule_config()
        actions_today = self.memory.get_today_action_count()

        schedule_line = ""
        if schedule:
            max_actions = schedule.get("max_actions_per_day", 25)
            schedule_line = (
                f"*Schedule:* {schedule['start_time']}–{schedule['end_time']}\n"
                f"*Actions today:* {actions_today}/{max_actions}\n"
            )
        else:
            schedule_line = "*Schedule:* not configured\n"

        narrative = self.memory.get_narrative()
        narrative_line = ""
        if narrative:
            snippet = narrative[:150] + "..." if len(narrative) > 150 else narrative
            narrative_line = f"*Agent state:* {snippet}\n"

        await update.message.reply_text(
            f"*Persona:* {self.persona_name}\n"
            f"*Provider:* {self.provider}\n"
            f"*Model:* `{model}`\n"
            f"*Messages stored:* {total_messages}\n"
            f"*Summaries:* {total_summaries}\n"
            f"{schedule_line}"
            f"*Pending triggers:* {len(active_triggers)}\n"
            f"{narrative_line}",
            parse_mode="Markdown",
        )

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        if self._chat_id is None:
            self._chat_id = update.effective_chat.id
            self.memory.set_tool_state("telegram", "chat_id", str(self._chat_id))
            logger.info(f"Chat ID captured and persisted: {self._chat_id}")

        user_text = update.message.text
        self.memory.add_message("user", user_text)

        system_prompt, messages = assemble_context(
            self.persona_prompt, self.memory, device=DEVICE_TELEGRAM
        )

        await update.message.chat.send_action(ChatAction.TYPING)

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

        # Strip schedule updates before sending to user.
        # Returns (clean_response, actions_json_or_none).
        clean_response, actions_json = strip_schedule_updates(response)

        self.memory.add_message("assistant", clean_response)

        # Send back — split if over Telegram's 4096 char limit
        if len(clean_response) <= 4096:
            try:
                await update.message.reply_text(clean_response, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(clean_response)
        else:
            for i in range(0, len(clean_response), 4096):
                await update.message.reply_text(clean_response[i:i + 4096])

        # Apply schedule updates silently
        if actions_json:
            logger.info("Applying schedule updates from user conversation")
            apply_schedule_updates(actions_json, self.memory)

        try:
            check_and_summarize(self.memory)
        except Exception as e:
            logger.error(f"Summarization error: {e}")

    # --- Entry Point ---

    def run(self):
        """Start the bot with long polling. Blocks forever."""
        app = Application.builder().token(self.token).build()
        self._app = app

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("provider", self.cmd_provider))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("clear", self.cmd_clear))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        ensure_agent_has_plan(self.memory)

        async def send_proactive(text: str):
            if self._chat_id is None:
                logger.warning("Cannot send proactive message: no chat ID yet.")
                return
            try:
                await app.bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode="Markdown",
                )
            except Exception:
                await app.bot.send_message(chat_id=self._chat_id, text=text)

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