"""
ScheduleTool — the agent manages its own wake-up schedule.

This tool exposes the triggers table as something the LLM can read
and manipulate. The agent uses it to plan its own day: scheduling
targeted wake-ups with specific purposes and tool requirements.

All methods are observe-tier because schedule management is internal
housekeeping, not a user-facing action. The agent doesn't need
approval to decide when to wake up next.

Validation logic (operating hours, future time checks, trigger
existence, tool name validation) lives here inside the tool, not
in the agent loop. The agent loop's generic validation gate handles
tool existence, method existence, tier permissions, and budget.
Tool-specific rules are the tool's responsibility.
"""

import json
import logging
from datetime import datetime
from tools.base import Tool, ToolMethod
from memory import PersonaMemory

logger = logging.getLogger(__name__)

# All tool names that exist in the system, even if not instantiated
# in the current process. Used to validate tool names in wake-ups.
# The CLI doesn't have TelegramTool (no send_fn) but should still
# allow scheduling wake-ups that use it.
KNOWN_TOOL_NAMES = {
    "schedule",
    "goals",
    "suggestions",
    "telegram",
    "google_calendar",
    "gmail",
}


class ScheduleTool(Tool):

    name = "schedule"
    description = (
        "Manage your own wake-up schedule. You can view your plan, "
        "add new wake-ups, modify existing ones, or cancel them. "
        "Each wake-up has a time, a purpose (your note to your future self), "
        "and a list of tools you'll need when you wake up."
    )

    def __init__(self, memory: PersonaMemory):
        self.memory = memory

    def get_context(self) -> str | None:
        """Return a formatted view of upcoming scheduled wake-ups."""
        return self._format_plan()

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="get_plan",
                description="View all upcoming scheduled wake-ups.",
                tier="observe",
            ),
            ToolMethod(
                name="add_wakeup",
                description=(
                    "Schedule a new wake-up. Provide a time, a purpose "
                    "(your note to your future self about what to do and why), "
                    "and which tools you'll need."
                ),
                tier="observe",
                parameters={
                    "time": {"type": "str", "description": "When to wake up, as 'YYYY-MM-DD HH:MM'", "required": True},
                    "purpose": {"type": "str", "description": "What to do when you wake up and why", "required": True},
                    "tools": {"type": "list[str]", "description": "Which tools to load (e.g. ['google_calendar', 'telegram']). Use [] for a planning cycle.", "required": True},
                },
            ),
            ToolMethod(
                name="modify_wakeup",
                description="Change the time, purpose, or tools of an existing wake-up.",
                tier="observe",
                parameters={
                    "id": {"type": "int", "description": "Trigger ID to modify", "required": True},
                    "time": {"type": "str", "description": "New time (optional)", "required": False},
                    "purpose": {"type": "str", "description": "New purpose (optional)", "required": False},
                    "tools": {"type": "list[str]", "description": "New tool list (optional)", "required": False},
                },
            ),
            ToolMethod(
                name="cancel_wakeup",
                description="Remove a scheduled wake-up.",
                tier="observe",
                parameters={
                    "id": {"type": "int", "description": "Trigger ID to cancel", "required": True},
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "get_plan":
            return self._format_plan() or "No upcoming wake-ups scheduled."
        elif method_name == "add_wakeup":
            return self._add_wakeup(**kwargs)
        elif method_name == "modify_wakeup":
            return self._modify_wakeup(**kwargs)
        elif method_name == "cancel_wakeup":
            return self._cancel_wakeup(**kwargs)
        else:
            raise ValueError(f"Unknown method '{method_name}' on ScheduleTool")

    # --- Validation ---

    def _validate_time(self, time_str: str) -> str:
        """
        Validate and normalize a time string.

        Checks:
            - Parseable as YYYY-MM-DD HH:MM[:SS]
            - In the future
            - Within operating hours (if schedule_config exists)

        Returns the normalized fire_at string (with seconds).
        Raises ValueError with a descriptive message on failure.
        """
        normalized = time_str.strip()
        if len(normalized) == 16:  # "YYYY-MM-DD HH:MM"
            normalized += ":00"

        try:
            fire_dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError(f"Invalid time format: '{time_str}'. Use 'YYYY-MM-DD HH:MM'.")

        now = datetime.now()
        if fire_dt <= now:
            raise ValueError(f"Time is in the past: {time_str}")

        # Check operating hours
        schedule = self.memory.get_schedule_config()
        if schedule:
            start_h, start_m = map(int, schedule["start_time"].split(":"))
            end_h, end_m = map(int, schedule["end_time"].split(":"))
            fire_minutes = fire_dt.hour * 60 + fire_dt.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            if fire_minutes < start_minutes or fire_minutes > end_minutes:
                raise ValueError(
                    f"Time {time_str} is outside operating hours "
                    f"({schedule['start_time']}–{schedule['end_time']})"
                )

        return normalized

    def _validate_tools(self, tools: list) -> list[str]:
        """
        Validate and normalize a tools list.

        Strips method names (e.g. "telegram.send_message" → "telegram"),
        checks all names are known, and deduplicates.

        Returns the normalized tools list.
        Raises ValueError on unknown tool names.
        """
        if not isinstance(tools, list):
            raise ValueError(f"Tools must be a list, got {type(tools).__name__}")

        normalized = []
        for t in tools:
            # Strip method name if present: "telegram.send_message" → "telegram"
            tool_name = t.split(".")[0] if isinstance(t, str) else str(t)
            if tool_name not in KNOWN_TOOL_NAMES:
                raise ValueError(f"Unknown tool '{tool_name}' in tools list")
            normalized.append(tool_name)

        # Deduplicate preserving order
        return list(dict.fromkeys(normalized))

    def _validate_trigger_exists(self, trigger_id: int) -> dict:
        """
        Validate that a trigger exists and hasn't fired.

        Returns the trigger dict.
        Raises ValueError if not found or already fired.
        """
        try:
            trigger_id = int(trigger_id)
        except (ValueError, TypeError):
            raise ValueError(f"Trigger ID must be an integer, got '{trigger_id}'")

        trigger = self.memory.get_trigger(trigger_id)
        if not trigger:
            raise ValueError(f"Trigger #{trigger_id} not found")
        if trigger.get("fired"):
            raise ValueError(f"Trigger #{trigger_id} has already fired")
        return trigger

    # --- Internal methods ---

    def _format_plan(self) -> str | None:
        """Format upcoming triggers into a readable plan view."""
        active = self.memory.get_active_triggers()
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        future = [t for t in active if t["fire_at"] > now_str]
        if not future:
            return None

        lines = ["YOUR SCHEDULED PLAN:"]
        for t in future:
            purpose = ""
            tools_str = ""
            try:
                ctx = json.loads(t["context"]) if t["context"] else {}
                purpose = ctx.get("purpose", t.get("context", ""))
                tools = ctx.get("tools", [])
                if tools:
                    tools_str = f" [{', '.join(tools)}]"
            except (json.JSONDecodeError, TypeError):
                purpose = t.get("context", "")

            fire_time = t["fire_at"]
            try:
                fire_dt = datetime.strptime(fire_time, "%Y-%m-%d %H:%M:%S")
                if fire_dt.date() == now.date():
                    time_display = f"Today {fire_dt.strftime('%H:%M')}"
                else:
                    time_display = fire_dt.strftime("%a %m/%d %H:%M")
            except ValueError:
                time_display = fire_time

            lines.append(f"  #{t['id']}  {time_display}  — {purpose}{tools_str}")

        return "\n".join(lines)

    def _add_wakeup(self, time: str, purpose: str, tools: list = None) -> str:
        """Schedule a new agent wake-up with full validation."""
        if tools is None:
            tools = []

        fire_at = self._validate_time(time)
        tools = self._validate_tools(tools)

        context = json.dumps({
            "purpose": purpose,
            "tools": tools,
        })

        trigger_id = self.memory.add_trigger(
            trigger_type="agent_cycle",
            fire_at=fire_at,
            context=context,
            recurring=None,
        )

        return f"Scheduled wake-up #{trigger_id} at {time}: {purpose}"

    def _modify_wakeup(self, id: int, time: str = None,
                       purpose: str = None, tools: list = None) -> str:
        """Modify an existing wake-up with full validation."""
        trigger = self._validate_trigger_exists(id)

        # Load existing context
        try:
            ctx = json.loads(trigger["context"]) if trigger["context"] else {}
        except (json.JSONDecodeError, TypeError):
            ctx = {"purpose": trigger.get("context", ""), "tools": []}

        # Apply and validate updates
        if purpose is not None:
            ctx["purpose"] = purpose
        if tools is not None:
            ctx["tools"] = self._validate_tools(tools)

        new_context = json.dumps(ctx)

        new_fire_at = trigger["fire_at"]
        if time is not None:
            new_fire_at = self._validate_time(time)

        self.memory.update_trigger(id, new_fire_at, new_context)
        return f"Modified wake-up #{id}"

    def _cancel_wakeup(self, id: int) -> str:
        """Cancel a scheduled wake-up with validation."""
        self._validate_trigger_exists(id)
        self.memory.delete_trigger(id)
        return f"Cancelled wake-up #{id}"
