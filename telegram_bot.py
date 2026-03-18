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
    6. It strips any <schedule_updates> from the response (Stage 5)
    7. It persists the response and sends it back through Telegram

Security:
    Only your Telegram user ID can talk to the bot. Everyone else
    gets silently ignored. Set TELEGRAM_ALLOWED_USER_ID in .env.

Memory:
    All messages are stored in data/<persona>/memory.db. Conversations
    survive restarts, reboots, and crashes. The /clear command wipes
    the database for this persona only.
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

logger = logging.getLogger(__name__)


def _strip_schedule_updates(response: str) -> tuple[str, list[str]]:
    """
    Strip <schedule_updates> tags from an LLM response.

    Returns (clean_response, schedule_lines).
    The clean_response has the tags removed and is what the user sees.
    The schedule_lines are the raw lines inside the tags for parsing.

    If no tags are present, returns (response, []).
    """
    pattern = r"<schedule_updates>(.*?)</schedule_updates>"
    match = re.search(pattern, response, re.DOTALL)

    if not match:
        return response.strip(), []

    # Extract the schedule commands
    schedule_block = match.group(1).strip()
    schedule_lines = [
        line.strip() for line in schedule_block.split("\n")
        if line.strip()
    ]

    # Remove the tags from the response
    clean = response[:match.start()] + response[match.end():]
    clean = clean.strip()

    return clean, schedule_lines


def _apply_schedule_updates(schedule_lines: list[str], memory: PersonaMemory):
    """
    Parse and apply schedule update commands from the LLM's response.

    Uses the same parsing logic as the agent cycle for consistency.
    Invalid commands are logged and skipped — the user's response is
    already sent by the time this runs, so failures are silent.
    """
    from agent import _parse_schedule_line, _validate_schedule_change
    from tools import create_tools

    tools = create_tools(memory)
    schedule_config = memory.get_schedule_config()
    registered_tools = set(tools.keys())

    for line in schedule_lines:
        parsed = _parse_schedule_line(line)
        if not parsed:
            logger.warning(f"Failed to parse schedule update: {line[:100]}")
            continue

        valid, reason = _validate_schedule_change(
            parsed, memory, schedule_config, registered_tools
        )

        if valid:
            schedule_tool = tools.get("schedule")
            if schedule_tool:
                try:
                    result = schedule_tool.execute(parsed["method"], **parsed["kwargs"])
                    logger.info(f"Schedule update applied: {result}")
                except Exception as e:
                    logger.error(f"Schedule update failed: {e}")
        else:
            logger.warning(f"Schedule update rejected: {reason} ({line[:80]})")


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

        # Telegram chat ID for proactive messaging. Priority:
        # 1. TELEGRAM_CHAT_ID from .env (most reliable, survives everything)
        # 2. Persisted in tool_state database (survives restarts)
        # 3. Captured on first authorized message (fallback)
        if config.TELEGRAM_CHAT_ID:
            self._chat_id: int | None = int(config.TELEGRAM_CHAT_ID)
        else:
            saved_chat_id = self.memory.get_tool_state("telegram", "chat_id")
            self._chat_id: int | None = int(saved_chat_id) if saved_chat_id else None
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

        # Show narrative state snippet
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

        # Capture chat ID for proactive messaging and persist it
        # so it survives service restarts.
        if self._chat_id is None:
            self._chat_id = update.effective_chat.id
            self.memory.set_tool_state("telegram", "chat_id", str(self._chat_id))
            logger.info(f"Chat ID captured and persisted: {self._chat_id}")

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

        # Stage 5: Strip schedule updates before sending to user.
        # The LLM may include <schedule_updates> tags when it detects
        # the user's message affects its plan.
        clean_response, schedule_lines = _strip_schedule_updates(response)

        # Persist the clean response (without schedule tags)
        self.memory.add_message("assistant", clean_response)

        # Send back — split if over Telegram's 4096 char limit
        if len(clean_response) <= 4096:
            # Try markdown first, fall back to plain text if it fails
            try:
                await update.message.reply_text(clean_response, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(clean_response)
        else:
            for i in range(0, len(clean_response), 4096):
                await update.message.reply_text(clean_response[i:i + 4096])

        # Stage 5: Apply schedule updates silently
        if schedule_lines:
            logger.info(f"Applying {len(schedule_lines)} schedule updates from user conversation")
            _apply_schedule_updates(schedule_lines, self.memory)

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

        # Bootstrap: ensure the agent has a plan (seeds first planning
        # cycle if none exists). Replaces the old seed_triggers() call.
        ensure_agent_has_plan(self.memory)

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