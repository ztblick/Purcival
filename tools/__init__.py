"""
Tool registry — discovers and manages available tools.

The agent loop gets its tool instances from here. To add a new tool,
import it and add it to the registry in create_tools().
"""

import logging

from tools.base import Tool
from tools.schedule_tool import ScheduleTool
from tools.telegram_tool import TelegramTool
from memory import PersonaMemory

logger = logging.getLogger(__name__)


def create_tools(memory: PersonaMemory, send_fn=None) -> dict[str, Tool]:
    """
    Create and return all available tool instances.

    Args:
        memory: The persona's memory instance. Passed to tools that
            need database access (ScheduleTool, GoogleCalendarTool).
        send_fn: Async function for sending Telegram messages. Required
            for TelegramTool. Can be None if running without Telegram
            (e.g., in tests).

    Returns:
        Dict mapping tool name to tool instance.
    """
    tools = {}

    # Schedule management — always available
    tools["schedule"] = ScheduleTool(memory)

    # Telegram — available when send_fn is provided
    if send_fn is not None:
        tools["telegram"] = TelegramTool(send_fn)

    # Google Calendar — available when credentials exist
    try:
        from google_auth import get_credentials, has_credentials

        if has_credentials(memory.persona_name):
            credentials = get_credentials(memory.persona_name)
            if credentials:
                from tools.google_calendar import GoogleCalendarTool
                tools["google_calendar"] = GoogleCalendarTool(memory, credentials)
                logger.info(
                    f"Google Calendar tool loaded for '{memory.persona_name}'"
                )
            else:
                logger.debug(
                    f"Google credentials not found for '{memory.persona_name}' "
                    f"— calendar tool not loaded"
                )
    except Exception as e:
        logger.warning(
            f"Failed to load Google Calendar tool for "
            f"'{memory.persona_name}': {e}"
        )

    # Future tools will be added here:
    # tools["gmail"] = GmailTool(memory, credentials)

    return tools