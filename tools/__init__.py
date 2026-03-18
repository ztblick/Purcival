"""
Tool registry — discovers and manages available tools.

The agent loop gets its tool instances from here. To add a new tool,
import it and add it to the registry in create_tools().
"""

from tools.base import Tool
from tools.schedule_tool import ScheduleTool
from tools.telegram_tool import TelegramTool
from memory import PersonaMemory


def create_tools(memory: PersonaMemory, send_fn=None) -> dict[str, Tool]:
    """
    Create and return all available tool instances.

    Args:
        memory: The persona's memory instance. Passed to tools that
            need database access (ScheduleTool, future GoogleCalendarTool).
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

    # Future tools will be added here:
    # tools["google_calendar"] = GoogleCalendarTool(memory, credentials)
    # tools["gmail"] = GmailTool(memory, credentials)

    return tools
