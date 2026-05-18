"""
Tool registry — discovers and manages available tools.
"""

import logging
from tools.base import Tool
from goals import SharedGoalStore
from tools.goal_tools import GoalTool, SuggestionTool
from tools.opportunity_tool import OpportunityTool
from tools.schedule_tool import ScheduleTool
from tools.telegram_tool import TelegramTool
from memory import PersonaMemory

logger = logging.getLogger(__name__)


def create_tools(
    memory: PersonaMemory,
    send_fn=None,
    goal_store: SharedGoalStore | None = None,
) -> dict[str, Tool]:
    tools = {}
    tools["schedule"] = ScheduleTool(memory)
    store = goal_store or SharedGoalStore()
    tools["goals"] = GoalTool(store)
    tools["opportunities"] = OpportunityTool(
        memory,
        store,
        created_by_persona=memory.persona_name,
    )
    tools["suggestions"] = SuggestionTool(
        store,
        created_by_persona=memory.persona_name,
        memory=memory,
    )
    if send_fn is not None:
        tools["telegram"] = TelegramTool(send_fn)
    try:
        from google_auth import get_credentials, has_credentials

        if has_credentials(memory.persona_name):
            credentials = get_credentials(memory.persona_name)
            if credentials:
                from tools.google_calendar import GoogleCalendarTool
                tools["google_calendar"] = GoogleCalendarTool(memory, credentials)
                logger.info(f"Google Calendar tool loaded for '{memory.persona_name}'")

                from tools.gmail import GmailTool
                tools["gmail"] = GmailTool(memory, credentials)
                logger.info(f"Gmail tool loaded for '{memory.persona_name}'")
    except Exception as e:
        logger.warning(f"Failed to load Google tools for '{memory.persona_name}': {e}")
    return tools
