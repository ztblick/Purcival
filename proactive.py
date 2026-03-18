"""
Proactive system — manages the scheduler and bootstraps the agent.

This module preserves the scheduler infrastructure (APScheduler checking
triggers every 60 seconds) and the schedule configuration system. But
the trigger handler now runs the full agent cycle instead of the old
compose-and-send pipeline.

The schedule itself (start time, end time) is stored in the database
in the schedule_config table. The agent manages its own wake-ups within
that window using the ScheduleTool.

Usage:
    from proactive import start_scheduler, ensure_agent_has_plan

    # At bot startup:
    ensure_agent_has_plan(memory)     # seed first planning cycle if needed
    start_scheduler(memory, send_fn)  # start the background check loop
"""

import json
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from memory import PersonaMemory
from agent import run_agent_cycle
from tools import create_tools

logger = logging.getLogger(__name__)


def ensure_agent_has_plan(memory: PersonaMemory):
    """
    Called at service startup. If no future triggers exist, seed a
    planning cycle so the agent can bootstrap its day.

    The agent takes it from there — the planning cycle discovers the
    day's events and emails, plans targeted wake-ups, and manages
    itself going forward.

    If no schedule is configured via /schedule, this does nothing.
    The persona operates in user-initiated-only mode.
    """
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    active = memory.get_active_triggers()
    future_triggers = [t for t in active if t["fire_at"] > now_str]

    if future_triggers:
        logger.info(
            f"Agent has {len(future_triggers)} future triggers, no bootstrap needed"
        )
        return

    schedule = memory.get_schedule_config()
    if not schedule:
        logger.info(
            f"No schedule configured for {memory.persona_name}. "
            f"Use /schedule in the terminal to set one."
        )
        return

    # Determine next wake-up time based on operating hours
    start_h, start_m = map(int, schedule["start_time"].split(":"))
    end_h, end_m = map(int, schedule["end_time"].split(":"))

    today_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    today_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    if now < today_start:
        next_time = today_start
    elif now < today_end:
        next_time = now + timedelta(minutes=1)
    else:
        next_time = today_start + timedelta(days=1)

    memory.add_trigger(
        trigger_type="agent_cycle",
        fire_at=next_time.strftime("%Y-%m-%d %H:%M:%S"),
        context=json.dumps({
            "purpose": "Planning cycle — review all tools and plan the day",
            "tools": [],
        }),
        recurring=None,
    )
    logger.info(f"Bootstrap: seeded planning cycle at {next_time}")


def start_scheduler(
    memory: PersonaMemory,
    send_fn,
    persona_prompt: str,
):
    """
    Start the background scheduler that checks for due triggers.

    Runs a check every 60 seconds. When triggers are due, processes
    them through the agent cycle.

    Args:
        memory: The persona's memory instance.
        send_fn: An async function that sends a message to the user
            via Telegram. Signature: async send_fn(text: str) -> None
        persona_prompt: The persona's system prompt, needed for
            context assembly during reasoning.

    Returns:
        The scheduler instance (for shutdown if needed).
    """
    scheduler = AsyncIOScheduler()

    async def check_triggers():
        due = memory.get_due_triggers()
        for trigger in due:
            try:
                # Create tools for this cycle
                tools = create_tools(memory, send_fn)

                # Run the full agent cycle
                await run_agent_cycle(
                    trigger=trigger,
                    memory=memory,
                    tools=tools,
                    persona_prompt=persona_prompt,
                    send_fn=send_fn,
                )
            except Exception as e:
                logger.error(
                    f"Error in agent cycle for trigger #{trigger['id']}: {e}",
                    exc_info=True,
                )

            # Mark trigger as fired regardless of cycle outcome.
            # The agent manages its own future triggers via ScheduleTool.
            memory.mark_trigger_fired(trigger["id"])

    scheduler.add_job(check_triggers, "interval", seconds=60)
    scheduler.start()
    logger.info("Agent scheduler started (checking every 60s)")
    return scheduler