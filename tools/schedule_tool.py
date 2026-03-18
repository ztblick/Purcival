"""
ScheduleTool — the agent manages its own wake-up schedule.

This tool exposes the triggers table as something the LLM can read
and manipulate. The agent uses it to plan its own day: scheduling
targeted wake-ups with specific purposes and tool requirements.

All methods are observe-tier because schedule management is internal
housekeeping, not a user-facing action. The agent doesn't need
approval to decide when to wake up next.
"""

import json
from datetime import datetime
from tools.base import Tool, ToolMethod
from memory import PersonaMemory


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
        """
        Return a formatted view of upcoming scheduled wake-ups.

        This is always included in the reasoning prompt so the LLM
        has awareness of its own future plan.
        """
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
                    "time": {
                        "type": "str",
                        "description": "When to wake up, as 'YYYY-MM-DD HH:MM'",
                        "required": True,
                    },
                    "purpose": {
                        "type": "str",
                        "description": "What to do when you wake up and why",
                        "required": True,
                    },
                    "tools": {
                        "type": "list[str]",
                        "description": "Which tools to load (e.g. ['google_calendar', 'telegram'])",
                        "required": True,
                    },
                },
            ),
            ToolMethod(
                name="modify_wakeup",
                description="Change the time, purpose, or tools of an existing wake-up.",
                tier="observe",
                parameters={
                    "id": {
                        "type": "int",
                        "description": "Trigger ID to modify",
                        "required": True,
                    },
                    "time": {
                        "type": "str",
                        "description": "New time (optional)",
                        "required": False,
                    },
                    "purpose": {
                        "type": "str",
                        "description": "New purpose (optional)",
                        "required": False,
                    },
                    "tools": {
                        "type": "list[str]",
                        "description": "New tool list (optional)",
                        "required": False,
                    },
                },
            ),
            ToolMethod(
                name="cancel_wakeup",
                description="Remove a scheduled wake-up.",
                tier="observe",
                parameters={
                    "id": {
                        "type": "int",
                        "description": "Trigger ID to cancel",
                        "required": True,
                    },
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

    # --- Internal methods ---

    def _format_plan(self) -> str | None:
        """Format upcoming triggers into a readable plan view."""
        active = self.memory.get_active_triggers()
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # Only show future triggers (unfired ones that haven't passed)
        future = [t for t in active if t["fire_at"] > now_str]

        if not future:
            return None

        lines = ["YOUR SCHEDULED PLAN:"]
        for t in future:
            # Parse trigger context for purpose and tools
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

            # Format the time nicely
            fire_time = t["fire_at"]
            try:
                fire_dt = datetime.strptime(fire_time, "%Y-%m-%d %H:%M:%S")
                if fire_dt.date() == now.date():
                    time_display = f"Today {fire_dt.strftime('%H:%M')}"
                elif fire_dt.date() == (now.date().__class__(
                    now.year, now.month, now.day
                )):
                    time_display = f"Today {fire_dt.strftime('%H:%M')}"
                else:
                    time_display = fire_dt.strftime("%a %m/%d %H:%M")
            except ValueError:
                time_display = fire_time

            lines.append(f"  #{t['id']}  {time_display}  — {purpose}{tools_str}")

        return "\n".join(lines)

    def _add_wakeup(self, time: str, purpose: str, tools: list = None) -> str:
        """Schedule a new agent wake-up."""
        if tools is None:
            tools = []

        # Normalize time format: accept "YYYY-MM-DD HH:MM" and add seconds
        fire_at = time.strip()
        if len(fire_at) == 16:  # "YYYY-MM-DD HH:MM"
            fire_at += ":00"

        # Build the trigger context
        is_planning = not tools  # Empty tools list = planning cycle
        context = json.dumps({
            "purpose": purpose,
            "tools": tools,
            "planning_cycle": is_planning,
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
        """Modify an existing wake-up."""
        trigger = self.memory.get_trigger(id)
        if not trigger:
            return f"Trigger #{id} not found."
        if trigger["fired"]:
            return f"Trigger #{id} has already fired."

        # Load existing context
        try:
            ctx = json.loads(trigger["context"]) if trigger["context"] else {}
        except (json.JSONDecodeError, TypeError):
            ctx = {"purpose": trigger.get("context", ""), "tools": [], "planning_cycle": False}

        # Apply updates
        if purpose is not None:
            ctx["purpose"] = purpose
        if tools is not None:
            ctx["tools"] = tools
            ctx["planning_cycle"] = not tools

        new_context = json.dumps(ctx)

        # Determine fire_at
        new_fire_at = trigger["fire_at"]
        if time is not None:
            new_fire_at = time.strip()
            if len(new_fire_at) == 16:
                new_fire_at += ":00"

        self.memory.update_trigger(id, new_fire_at, new_context)
        return f"Modified wake-up #{id}"

    def _cancel_wakeup(self, id: int) -> str:
        """Cancel a scheduled wake-up."""
        trigger = self.memory.get_trigger(id)
        if not trigger:
            return f"Trigger #{id} not found."
        if trigger["fired"]:
            return f"Trigger #{id} has already fired."

        self.memory.delete_trigger(id)
        return f"Cancelled wake-up #{id}"
