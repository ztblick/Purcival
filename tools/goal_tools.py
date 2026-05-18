"""Goal and suggestion tools for the Goals dashboard agent integration."""

from __future__ import annotations

from collections import defaultdict

from accountability import record_step_status_change
from goals import STEP_STATUSES, SharedGoalStore
from memory import PersonaMemory
from tools.base import Tool, ToolMethod


class GoalTool(Tool):
    """Expose active goals and step state to the agent loop."""

    name = "goals"
    description = "Read Zach's active goals and current goal-linked steps."

    def __init__(self, store: SharedGoalStore | None = None):
        self.store = store or SharedGoalStore()

    def get_context(self) -> str | None:
        return self._format_goals_context()

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="list_goals",
                description="List goals, optionally filtered by status or category.",
                tier="observe",
                parameters={
                    "status": {"type": "str", "description": "Goal status filter, such as active or paused", "required": False},
                    "category": {"type": "str", "description": "Goal category filter", "required": False},
                },
            ),
            ToolMethod(
                name="list_steps",
                description="List steps, optionally filtered by goal id or status.",
                tier="observe",
                parameters={
                    "goal_id": {"type": "int", "description": "Goal id filter", "required": False},
                    "status": {"type": "str", "description": "Step status filter, such as suggested or accepted", "required": False},
                },
            ),
            ToolMethod(
                name="get_goal_detail",
                description="Show one goal with its linked steps.",
                tier="observe",
                parameters={
                    "goal_id": {"type": "int", "description": "Goal id to inspect", "required": True},
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "list_goals":
            return self._format_goal_rows(
                self.store.list_goals(
                    status=kwargs.get("status"),
                    category=kwargs.get("category"),
                )
            )
        if method_name == "list_steps":
            return self._format_step_rows(
                self.store.list_steps(
                    goal_id=kwargs.get("goal_id"),
                    status=kwargs.get("status"),
                )
            )
        if method_name == "get_goal_detail":
            return self._format_goal_detail(int(kwargs["goal_id"]))
        raise ValueError(f"Unknown method '{method_name}' on GoalTool")

    def _format_goals_context(self) -> str | None:
        goals = self.store.list_goals(status="active")
        if not goals:
            return None

        steps_by_goal = defaultdict(list)
        for step in self.store.list_steps():
            steps_by_goal[step["goal_id"]].append(step)

        lines = ["GOALS"]
        for category, category_goals in _group_goals_by_category(goals).items():
            lines.append("")
            lines.append(category.title())
            for goal in category_goals:
                lines.append(f"  #{goal['id']} {goal['title']}")
                if goal.get("description"):
                    lines.append(f"    Context: {goal['description']}")
                goal_steps = steps_by_goal.get(goal["id"], [])
                lines.extend(_format_status_group(goal_steps, "accepted", "Accepted steps"))
                lines.extend(_format_status_group(goal_steps, "suggested", "Open suggestions"))
                lines.extend(_format_status_group(goal_steps, "rejected", "Recently rejected", limit=3))
                lines.extend(_format_status_group(goal_steps, "completed", "Recently completed", limit=3))

        return "\n".join(lines)

    def _format_goal_rows(self, goals: list[dict]) -> str:
        if not goals:
            return "No goals found."
        return "\n".join(
            f"#{goal['id']} [{goal['status']}] {goal['category']}: {goal['title']}"
            for goal in goals
        )

    def _format_step_rows(self, steps: list[dict]) -> str:
        if not steps:
            return "No steps found."
        return "\n".join(_format_step_line(step) for step in steps)

    def _format_goal_detail(self, goal_id: int) -> str:
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"Goal {goal_id} does not exist")

        lines = [
            f"Goal #{goal['id']}: {goal['title']}",
            f"Category: {goal['category']}",
            f"Status: {goal['status']}",
        ]
        if goal.get("description"):
            lines.append(f"Description: {goal['description']}")

        steps = self.store.list_steps(goal_id=goal_id)
        if steps:
            lines.append("Steps:")
            lines.extend(f"  {_format_step_line(step)}" for step in steps)
        else:
            lines.append("Steps: none")
        return "\n".join(lines)


class SuggestionTool(Tool):
    """Allow the agent to create and maintain candidate goal steps."""

    name = "suggestions"
    description = "Propose and manage one-shot suggested steps for existing goals."

    def __init__(
        self,
        store: SharedGoalStore | None = None,
        created_by_persona: str = "jo",
        memory: PersonaMemory | None = None,
    ):
        self.store = store or SharedGoalStore()
        self.created_by_persona = created_by_persona
        self.memory = memory

    def get_context(self) -> str | None:
        return self._format_recent_signal()

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="propose_suggestion",
                description=(
                    "Create a candidate one-shot step under an existing active goal. "
                    "The new step is suggested, not accepted."
                ),
                tier="observe",
                parameters={
                    "goal_id": {"type": "int", "description": "Existing active goal id", "required": True},
                    "title": {"type": "str", "description": "Concrete one-shot step Zach can accept or reject", "required": True},
                    "description": {"type": "str", "description": "Optional extra detail", "required": False},
                    "rationale": {"type": "str", "description": "Why this step is useful now", "required": False},
                },
            ),
            ToolMethod(
                name="list_suggestions",
                description="List suggested steps, optionally filtered by status or goal id.",
                tier="observe",
                parameters={
                    "status": {"type": "str", "description": "Step status filter; defaults to suggested", "required": False},
                    "goal_id": {"type": "int", "description": "Goal id filter", "required": False},
                },
            ),
            ToolMethod(
                name="update_status",
                description=(
                    "Update a step status as a trusted internal write and "
                    "record an event-backed receipt."
                ),
                tier="internal_write",
                parameters={
                    "step_id": {"type": "int", "description": "Step id to update", "required": True},
                    "status": {"type": "str", "description": "New status: suggested, accepted, rejected, completed, or abandoned", "required": True},
                    "note": {"type": "str", "description": "Optional note to store with the update", "required": False},
                },
            ),
            ToolMethod(
                name="complete_step",
                description="Mark an accepted step completed with an event-backed receipt.",
                tier="internal_write",
                parameters={
                    "step_id": {"type": "int", "description": "Step id to complete", "required": True},
                    "note": {"type": "str", "description": "Optional completion note", "required": False},
                },
            ),
            ToolMethod(
                name="abandon_step",
                description="Mark an accepted step abandoned with an event-backed receipt.",
                tier="internal_write",
                parameters={
                    "step_id": {"type": "int", "description": "Step id to abandon", "required": True},
                    "note": {"type": "str", "description": "Optional reason or evidence", "required": False},
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "propose_suggestion":
            return self._propose_suggestion(**kwargs)
        if method_name == "list_suggestions":
            return self._list_suggestions(**kwargs)
        if method_name == "update_status":
            return self._update_status(**kwargs)
        if method_name == "complete_step":
            return self._update_status(
                step_id=kwargs["step_id"],
                status="completed",
                note=kwargs.get("note"),
            )
        if method_name == "abandon_step":
            return self._update_status(
                step_id=kwargs["step_id"],
                status="abandoned",
                note=kwargs.get("note"),
            )
        raise ValueError(f"Unknown method '{method_name}' on SuggestionTool")

    def _propose_suggestion(
        self,
        goal_id: int,
        title: str,
        description: str | None = None,
        rationale: str | None = None,
    ) -> str:
        goal_id = int(goal_id)
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"Goal {goal_id} does not exist")
        if goal["status"] != "active":
            raise ValueError(f"Goal {goal_id} is not active")

        title = title.strip()
        if self._matches_existing_open_step(goal_id, title):
            raise ValueError("A suggested or accepted step with that title already exists for this goal")

        step_id = self.store.create_step(
            goal_id=goal_id,
            title=title,
            description=description,
            rationale=rationale,
            status="suggested",
            source="agent_planning",
            created_by_persona=self.created_by_persona,
        )
        return f"Created suggested step #{step_id} for goal #{goal_id}: {title}"

    def _list_suggestions(
        self,
        status: str | None = None,
        goal_id: int | None = None,
    ) -> str:
        status = status or "suggested"
        steps = self.store.list_steps(goal_id=goal_id, status=status)
        if not steps:
            return f"No {status} steps found."
        return "\n".join(_format_step_line(step) for step in steps)

    def _update_status(self, step_id: int, status: str, note: str | None = None) -> str:
        if status not in STEP_STATUSES:
            choices = ", ".join(sorted(STEP_STATUSES))
            raise ValueError(f"Invalid status '{status}'. Expected one of: {choices}")

        receipt = record_step_status_change(
            store=self.store,
            memory=self.memory,
            step_id=int(step_id),
            status=status,
            source="suggestion_tool",
            actor=self.created_by_persona,
            note=note,
        )
        step_id = receipt["step_id"]
        return f"Updated step #{step_id} to {status}"

    def _matches_existing_open_step(self, goal_id: int, title: str) -> bool:
        normalized = title.casefold()
        for status in ("suggested", "accepted"):
            for step in self.store.list_steps(goal_id=goal_id, status=status):
                if step["title"].casefold() == normalized:
                    return True
        return False

    def _format_recent_signal(self) -> str | None:
        accepted = self.store.list_steps(status="accepted")[:5]
        rejected = self.store.list_steps(status="rejected")[:5]
        if not accepted and not rejected:
            return None

        lines = ["SUGGESTION FEEDBACK"]
        if accepted:
            lines.append("Recently accepted:")
            lines.extend(f"  {_format_step_line(step)}" for step in accepted)
        if rejected:
            lines.append("Recently rejected:")
            lines.extend(f"  {_format_step_line(step)}" for step in rejected)
        return "\n".join(lines)


def _group_goals_by_category(goals: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for goal in goals:
        grouped[goal["category"]].append(goal)
    return dict(grouped)


def _format_status_group(
    steps: list[dict],
    status: str,
    label: str,
    limit: int = 5,
) -> list[str]:
    matching = [step for step in steps if step["status"] == status][:limit]
    if not matching:
        return [f"    {label}: none"]
    lines = [f"    {label}:"]
    lines.extend(f"      {_format_step_line(step)}" for step in matching)
    return lines


def _format_step_line(step: dict) -> str:
    rationale = f" - {step['rationale']}" if step.get("rationale") else ""
    return f"#{step['id']} [{step['status']}] {step['title']}{rationale}"
