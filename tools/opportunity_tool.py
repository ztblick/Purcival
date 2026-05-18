"""Opportunity tool for the core agent reliability redesign."""

from __future__ import annotations

import json

from goals import SharedGoalStore
from memory import (
    AGENT_OPPORTUNITY_SUPPRESSION_STATUSES,
    PersonaMemory,
)
from tools.base import Tool, ToolMethod


class OpportunityTool(Tool):
    """Store and deliver low-risk internal opportunities."""

    name = "opportunities"
    description = "Create and manage candidate ways Purcival might help."

    def __init__(
        self,
        memory: PersonaMemory,
        store: SharedGoalStore | None = None,
        created_by_persona: str = "jo",
    ):
        self.memory = memory
        self.store = store or SharedGoalStore()
        self.created_by_persona = created_by_persona
        self.evidence_event_ids: list[int] = []

    def set_evidence_event_ids(self, event_ids: list[int]):
        """Attach the current cycle's recorded observations to new opportunities."""
        self.evidence_event_ids = list(event_ids)

    def get_context(self) -> str | None:
        lines = []
        active = self.memory.list_agent_opportunities(limit=8)
        visible = [
            opportunity
            for opportunity in active
            if opportunity["status"] in {"candidate", "queued", "scheduled", "delivered"}
        ]
        suppressed = [
            opportunity
            for opportunity in active
            if opportunity["status"] in {"rejected", "dismissed", "blocked"}
        ][:5]

        if visible:
            lines.append("OPEN OPPORTUNITIES")
            lines.extend(f"  {_format_opportunity_line(item)}" for item in visible)
        if suppressed:
            if lines:
                lines.append("")
            lines.append("RECENTLY SUPPRESSED OPPORTUNITIES")
            lines.extend(f"  {_format_opportunity_line(item)}" for item in suppressed)

        return "\n".join(lines) if lines else None

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="propose_goal_step",
                description=(
                    "Record a possible goal-linked step as an opportunity. "
                    "Low-risk, non-duplicate opportunities are delivered as "
                    "dashboard suggestions."
                ),
                tier="internal_write",
                parameters={
                    "goal_id": {"type": "int", "description": "Existing active goal id", "required": True},
                    "title": {"type": "str", "description": "Concrete one-shot step Zach can accept or reject", "required": True},
                    "rationale": {"type": "str", "description": "Why this opportunity is useful now", "required": True},
                    "description": {"type": "str", "description": "Optional extra detail for the suggested step", "required": False},
                    "urgency": {"type": "int", "description": "0-5 urgency score", "required": False},
                    "impact": {"type": "int", "description": "0-5 impact score", "required": False},
                    "confidence": {"type": "int", "description": "0-5 confidence score", "required": False},
                    "attention_cost": {"type": "int", "description": "0-5 attention-cost score", "required": False},
                },
            ),
            ToolMethod(
                name="list_opportunities",
                description="List stored opportunities, optionally filtered by status or goal id.",
                tier="observe",
                parameters={
                    "status": {"type": "str", "description": "Opportunity status filter", "required": False},
                    "goal_id": {"type": "int", "description": "Goal id filter", "required": False},
                },
            ),
            ToolMethod(
                name="dismiss_opportunity",
                description="Dismiss an opportunity so similar repeats are suppressed.",
                tier="internal_write",
                parameters={
                    "opportunity_id": {"type": "int", "description": "Opportunity id to dismiss", "required": True},
                    "reason": {"type": "str", "description": "Short reason for suppression", "required": False},
                },
            ),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "propose_goal_step":
            return self._propose_goal_step(**kwargs)
        if method_name == "list_opportunities":
            return self._list_opportunities(**kwargs)
        if method_name == "dismiss_opportunity":
            return self._dismiss_opportunity(**kwargs)
        raise ValueError(f"Unknown method '{method_name}' on OpportunityTool")

    def _propose_goal_step(
        self,
        goal_id: int,
        title: str,
        rationale: str,
        description: str | None = None,
        urgency: int = 3,
        impact: int = 3,
        confidence: int = 3,
        attention_cost: int = 2,
    ) -> str:
        goal_id = int(goal_id)
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"Goal {goal_id} does not exist")
        if goal["status"] != "active":
            raise ValueError(f"Goal {goal_id} is not active")

        title = _require_text(title, "title")
        rationale = _require_text(rationale, "rationale")
        duplicate_key = _duplicate_key(goal_id, title)
        existing = self.memory.get_agent_opportunity_by_duplicate_key(duplicate_key)
        if existing:
            if existing["status"] in AGENT_OPPORTUNITY_SUPPRESSION_STATUSES:
                return (
                    f"Suppressed similar opportunity #{existing['id']} "
                    f"because it is {existing['status']}."
                )
            self.memory.update_agent_opportunity(
                existing["id"],
                rationale=rationale,
                urgency=urgency,
                impact=impact,
                confidence=confidence,
                attention_cost=attention_cost,
                proposed_action=_proposed_step_action(
                    goal_id,
                    title,
                    description,
                    rationale,
                ),
            )
            refreshed = self.memory.get_agent_opportunity(existing["id"])
            if refreshed and refreshed.get("step_id"):
                return (
                    f"Updated opportunity #{existing['id']}; already delivered "
                    f"as suggested step #{refreshed['step_id']}."
                )
            return f"Updated queued opportunity #{existing['id']}: {title}"

        duplicate_step = _find_open_step(self.store, goal_id, title)
        if duplicate_step is not None:
            opportunity_id = self.memory.add_agent_opportunity(
                kind="suggest_goal_step",
                title=title,
                rationale=rationale,
                evidence_event_ids=self.evidence_event_ids,
                goal_id=goal_id,
                step_id=duplicate_step["id"],
                status="blocked",
                urgency=urgency,
                impact=impact,
                confidence=confidence,
                attention_cost=attention_cost,
                risk_level="low",
                proposed_action={
                    **_proposed_step_action(goal_id, title, description, rationale),
                    "blocked_reason": "matching_open_step",
                },
                duplicate_key=duplicate_key,
            )
            return (
                f"Suppressed opportunity #{opportunity_id}; matching open step "
                f"#{duplicate_step['id']} already exists."
            )

        proposed_action = _proposed_step_action(goal_id, title, description, rationale)
        opportunity_id = self.memory.add_agent_opportunity(
            kind="suggest_goal_step",
            title=title,
            rationale=rationale,
            evidence_event_ids=self.evidence_event_ids,
            goal_id=goal_id,
            status="queued",
            urgency=urgency,
            impact=impact,
            confidence=confidence,
            attention_cost=attention_cost,
            risk_level="low",
            proposed_action=proposed_action,
            duplicate_key=duplicate_key,
        )

        if int(confidence) < 2:
            return f"Queued opportunity #{opportunity_id} for later review: {title}"

        step_id = self.store.create_step(
            goal_id=goal_id,
            title=title,
            description=description,
            rationale=rationale,
            status="suggested",
            source="agent_planning",
            created_by_persona=self.created_by_persona,
        )
        delivered_action = {**proposed_action, "delivered_step_id": step_id}
        self.memory.update_agent_opportunity(
            opportunity_id,
            status="delivered",
            step_id=step_id,
            proposed_action=delivered_action,
        )
        self.memory.add_agent_event(
            event_type="opportunity_delivered",
            source="agent_opportunity",
            source_id=str(opportunity_id),
            payload={
                "opportunity_id": opportunity_id,
                "kind": "suggest_goal_step",
                "goal_id": goal_id,
                "step_id": step_id,
                "title": title,
            },
        )
        return (
            f"Recorded opportunity #{opportunity_id} and delivered suggested "
            f"step #{step_id} for goal #{goal_id}: {title}"
        )

    def _list_opportunities(
        self,
        status: str | None = None,
        goal_id: int | None = None,
    ) -> str:
        opportunities = self.memory.list_agent_opportunities(
            status=status,
            goal_id=goal_id,
        )
        if not opportunities:
            return "No opportunities found."
        return "\n".join(_format_opportunity_line(item) for item in opportunities)

    def _dismiss_opportunity(
        self,
        opportunity_id: int,
        reason: str | None = None,
    ) -> str:
        opportunity_id = int(opportunity_id)
        opportunity = self.memory.get_agent_opportunity(opportunity_id)
        if opportunity is None:
            raise ValueError(f"Opportunity {opportunity_id} does not exist")

        proposed_action = _decode_action(opportunity.get("proposed_action_json"))
        if reason:
            proposed_action["dismiss_reason"] = reason.strip()
        self.memory.update_agent_opportunity(
            opportunity_id,
            status="dismissed",
            proposed_action=proposed_action,
        )
        return f"Dismissed opportunity #{opportunity_id}"


def _duplicate_key(goal_id: int, title: str) -> str:
    normalized = " ".join(title.casefold().split())
    return f"suggest_goal_step:goal={goal_id}:{normalized}"


def _find_open_step(
    store: SharedGoalStore,
    goal_id: int,
    title: str,
) -> dict | None:
    normalized = title.casefold()
    for status in ("suggested", "accepted"):
        for step in store.list_steps(goal_id=goal_id, status=status):
            if step["title"].casefold() == normalized:
                return step
    return None


def _proposed_step_action(
    goal_id: int,
    title: str,
    description: str | None,
    rationale: str,
) -> dict:
    return {
        "tool": "suggestions",
        "method": "propose_suggestion",
        "parameters": {
            "goal_id": goal_id,
            "title": title,
            "description": description,
            "rationale": rationale,
        },
    }


def _decode_action(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _require_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be empty")
    return cleaned


def _format_opportunity_line(opportunity: dict) -> str:
    goal = f" goal #{opportunity['goal_id']}" if opportunity.get("goal_id") else ""
    step = f" step #{opportunity['step_id']}" if opportunity.get("step_id") else ""
    scores = (
        f"u{opportunity['urgency']}/i{opportunity['impact']}/"
        f"c{opportunity['confidence']}/a{opportunity['attention_cost']}"
    )
    return (
        f"#{opportunity['id']} [{opportunity['status']}] "
        f"{opportunity['kind']}{goal}{step}: {opportunity['title']} ({scores})"
    )
