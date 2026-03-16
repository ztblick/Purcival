"""
Proactive messaging — enables the assistant to initiate conversations.

This module manages scheduled wake-ups that allow a persona to message
the user without being prompted. Three layers, cleanly separated:

    1. Scheduler — manages when triggers fire (clock layer)
    2. Decision gate — decides whether to actually send a message
    3. Message composer — assembles context and asks the LLM what to say

Each persona runs its own scheduler within its own process. Triggers
are stored in the persona's SQLite database and survive restarts.

Trigger types (tiered decision logic):
    - "reminder"  → always send, LLM composes the message
    - "calendar"  → always send, LLM composes with event context
    - "check_in"  → LLM decides whether to send AND composes

Usage:
    from proactive import start_scheduler, seed_hourly_triggers

    # At bot startup:
    seed_hourly_triggers(memory)      # ensure recurring triggers exist
    start_scheduler(memory, send_fn)  # start the background check loop
"""

import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import brain
from memory import PersonaMemory
from context import assemble_context, DEVICE_TELEGRAM

logger = logging.getLogger(__name__)

# --- Configuration ---

# Hours during which check-in triggers can fire (inclusive).
WAKE_HOURS_START = 6
WAKE_HOURS_END = 23

# Don't send proactive messages if the user sent a message within
# this many minutes. Avoids interrupting active conversations.
ACTIVE_CONVERSATION_MINUTES = 15

# Provider for composing proactive messages.
PROACTIVE_PROVIDER = "ollama"


# --- Layer 1: Scheduler ---

def seed_hourly_triggers(memory: PersonaMemory):
    """
    Ensure recurring hourly check-in triggers exist for today and tomorrow.

    Called at bot startup. Creates triggers on the hour from
    WAKE_HOURS_START to WAKE_HOURS_END for today (any not yet passed)
    and all of tomorrow. Skips times that already have a pending trigger
    to avoid duplicates on restart.

    This function is idempotent — safe to call multiple times.
    """
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    # Get existing unfired trigger times to avoid duplicates
    active = memory.get_active_triggers()
    existing_times = set()
    for t in active:
        if t["recurring"] == "hourly_check_in":
            existing_times.add(t["fire_at"])

    created = 0
    for day in [today, tomorrow]:
        for hour in range(WAKE_HOURS_START, WAKE_HOURS_END + 1):
            fire_at = datetime(day.year, day.month, day.day, hour, 0, 0)

            # Skip times in the past
            if fire_at <= now:
                continue

            fire_at_str = fire_at.strftime("%Y-%m-%d %H:%M:%S")

            # Skip if trigger already exists for this time
            if fire_at_str in existing_times:
                continue

            memory.add_trigger(
                trigger_type="check_in",
                fire_at=fire_at_str,
                context="Scheduled hourly check-in",
                recurring="hourly_check_in",
            )
            created += 1

    if created > 0:
        logger.info(f"Seeded {created} hourly check-in triggers")


def _advance_daily_triggers(memory: PersonaMemory):
    """
    Called once per day (or on any trigger fire) to ensure tomorrow's
    triggers exist. This keeps the trigger pool replenished without
    creating weeks of future triggers.
    """
    seed_hourly_triggers(memory)


def start_scheduler(
    memory: PersonaMemory,
    send_fn,
    persona_prompt: str,
):
    """
    Start the background scheduler that checks for due triggers.

    Runs a check every 60 seconds. When triggers are due, processes
    them through the decision gate and message composer.

    Args:
        memory: The persona's memory instance.
        send_fn: An async function that sends a message to the user
            via Telegram. Signature: async send_fn(text: str) -> None
        persona_prompt: The persona's system prompt, needed for
            context assembly during message composition.

    Returns:
        The scheduler instance (for shutdown if needed).
    """
    scheduler = AsyncIOScheduler()

    async def check_triggers():
        due = memory.get_due_triggers()
        for trigger in due:
            try:
                await _process_trigger(trigger, memory, send_fn, persona_prompt)
            except Exception as e:
                logger.error(f"Error processing trigger #{trigger['id']}: {e}")

            # Mark as handled
            if trigger["recurring"]:
                # Advance recurring triggers and replenish pool
                memory.mark_trigger_fired(trigger["id"])
                _advance_daily_triggers(memory)
            else:
                memory.mark_trigger_fired(trigger["id"])

    scheduler.add_job(check_triggers, "interval", seconds=60)
    scheduler.start()
    logger.info("Proactive scheduler started (checking every 60s)")
    return scheduler


# --- Layer 2: Decision Gate ---

def _should_send(trigger: dict, memory: PersonaMemory) -> bool:
    """
    Decide whether a trigger should result in a message being sent.

    Tiered logic by trigger type:
        - "reminder" → always send
        - "calendar" → always send
        - "check_in" → send unless conversation is already active

    Args:
        trigger: The trigger dict from the database.
        memory: The persona's memory instance.

    Returns:
        True if a message should be sent, False to skip silently.
    """
    trigger_type = trigger["type"]

    # Reminders and calendar triggers always fire
    if trigger_type in ("reminder", "calendar"):
        return True

    # Check-ins: skip if conversation is already active
    if trigger_type == "check_in":
        recent = memory.get_recent_messages(limit=1)
        if recent:
            last_message_time = recent[0].get("created_at", "")
            if last_message_time:
                try:
                    last_time = datetime.strptime(last_message_time, "%Y-%m-%d %H:%M:%S")
                    minutes_since = (datetime.now() - last_time).total_seconds() / 60
                    if minutes_since < ACTIVE_CONVERSATION_MINUTES:
                        logger.info(
                            f"Skipping check-in: last message was {minutes_since:.0f}m ago"
                        )
                        return False
                except ValueError:
                    pass  # Can't parse timestamp, proceed anyway

        return True

    # Unknown trigger type — send to be safe
    logger.warning(f"Unknown trigger type: {trigger_type}")
    return True


# --- Layer 3: Message Composer ---

def _build_proactive_prompt(trigger: dict, memory: PersonaMemory) -> str:
    """
    Build a system prompt addendum for proactive messages.

    This gets appended to the persona's normal system prompt to give
    the LLM context about why it's reaching out and what to consider.

    Different trigger types get different prompts — this is the
    extensibility point for future trigger types like calendar events.
    """
    now = datetime.now()
    time_str = now.strftime("%A, %B %d at %I:%M %p")

    # Find when the last conversation happened
    recent = memory.get_recent_messages(limit=1)
    if recent and recent[0].get("created_at"):
        try:
            last_time = datetime.strptime(recent[0]["created_at"], "%Y-%m-%d %H:%M:%S")
            delta = now - last_time
            hours = delta.total_seconds() / 3600
            if hours < 1:
                time_since = f"{int(delta.total_seconds() / 60)} minutes"
            elif hours < 24:
                time_since = f"{hours:.1f} hours"
            else:
                time_since = f"{delta.days} days"
            last_msg_note = (
                f"The last message in this conversation was {time_since} ago."
            )
        except ValueError:
            last_msg_note = ""
    else:
        last_msg_note = "There are no previous messages in this conversation."

    trigger_context = trigger.get("context") or ""

    if trigger["type"] == "reminder":
        return (
            f"\n\n---\n\n## PROACTIVE MESSAGE\n\n"
            f"You are sending a reminder to Zach. It is {time_str}.\n"
            f"{last_msg_note}\n\n"
            f"Reminder: {trigger_context}\n\n"
            f"Deliver this reminder naturally and warmly. Be brief."
        )

    elif trigger["type"] == "calendar":
        return (
            f"\n\n---\n\n## PROACTIVE MESSAGE\n\n"
            f"You are reaching out to Zach about a calendar event. "
            f"It is {time_str}. {last_msg_note}\n\n"
            f"Event: {trigger_context}\n\n"
            f"Ask how it went, share relevant thoughts, or provide "
            f"useful context. Be brief and natural."
        )

    else:  # check_in
        return (
            f"\n\n---\n\n## PROACTIVE MESSAGE\n\n"
            f"You are waking up on a schedule. It is {time_str}. "
            f"{last_msg_note}\n\n"
            f"Consider whether there is anything Zach would find useful "
            f"to know, be reminded of, or appreciate hearing right now. "
            f"Think about the time of day, what he might be doing, any "
            f"upcoming events or commitments, and the flow of recent "
            f"conversations.\n\n"
            f"Send a brief, natural message. Do not announce that you "
            f"are an AI waking up on a schedule — just reach out like "
            f"a thoughtful assistant would."
        )


async def _process_trigger(
    trigger: dict,
    memory: PersonaMemory,
    send_fn,
    persona_prompt: str,
):
    """
    Full pipeline: decision gate → compose → send.

    If the decision gate says no, logs and returns silently.
    If yes, composes a message using the LLM and sends it.
    The proactive message and the LLM's response are both
    persisted to the database so the conversation can continue
    naturally if the user replies.
    """
    if not _should_send(trigger, memory):
        return

    logger.info(
        f"Processing trigger #{trigger['id']} "
        f"(type={trigger['type']}, context={trigger.get('context', '')})"
    )

    # Build the augmented system prompt
    proactive_addendum = _build_proactive_prompt(trigger, memory)
    augmented_prompt = persona_prompt + proactive_addendum

    # Assemble context (includes summaries, user context, recent messages)
    system_prompt, messages = assemble_context(
        augmented_prompt, memory, device=DEVICE_TELEGRAM
    )

    # The LLM generates the proactive message as an "assistant" turn.
    # We send an empty-ish user message to prompt it.
    messages.append({
        "role": "user",
        "content": "[This is an automated wake-up. Compose your message to Zach.]",
    })

    try:
        response = brain.ask(
            messages,
            system=system_prompt,
            provider=PROACTIVE_PROVIDER,
        )
    except Exception as e:
        logger.error(f"Proactive message generation failed: {e}")
        return

    response = response.strip()
    if not response:
        logger.info("LLM returned empty response, skipping send")
        return

    # Persist the assistant's proactive message so it appears in
    # conversation history. The user can reply naturally.
    memory.add_message("assistant", response)

    # Send via Telegram
    try:
        await send_fn(response)
        logger.info(f"Proactive message sent: {response[:80]}...")
    except Exception as e:
        logger.error(f"Failed to send proactive message: {e}")
