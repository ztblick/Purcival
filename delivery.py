"""Dashboard delivery helpers for agent opportunities."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from goals import SharedGoalStore
from memory import PersonaMemory


INBOX_ACTIVE_STATUSES = {"unread", "snoozed"}


def deliver_opportunity_to_inbox(
    memory: PersonaMemory,
    store: SharedGoalStore,
    opportunity: dict[str, Any],
) -> int | None:
    """Create or refresh the dashboard inbox card for an opportunity."""
    card = _card_from_opportunity(store, opportunity)
    if card is None:
        return None

    duplicate_key = f"inbox:opportunity={opportunity['id']}"
    existing = memory.get_agent_inbox_item_by_duplicate_key(duplicate_key)
    if existing:
        if existing["status"] in INBOX_ACTIVE_STATUSES:
            memory.update_agent_inbox_item(
                existing["id"],
                priority=card["priority"],
                title=card["title"],
                body=card["body"],
                actions=card["actions"],
            )
        return int(existing["id"])

    item_id = memory.add_agent_inbox_item(
        opportunity_id=int(opportunity["id"]),
        priority=card["priority"],
        surface="dashboard",
        title=card["title"],
        body=card["body"],
        actions=card["actions"],
        duplicate_key=duplicate_key,
        expires_at=card.get("expires_at"),
    )
    memory.add_agent_event(
        event_type="inbox_item_created",
        source="agent_inbox",
        source_id=str(item_id),
        payload={
            "inbox_item_id": item_id,
            "opportunity_id": opportunity["id"],
            "kind": opportunity["kind"],
            "title": card["title"],
            "surface": "dashboard",
        },
    )
    return item_id


def mark_inbox_item(
    memory: PersonaMemory,
    item_id: int,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark an inbox item acted/dismissed/expired and log the outcome."""
    item = memory.get_agent_inbox_item(int(item_id))
    if item is None:
        raise ValueError(f"Inbox item {item_id} does not exist")
    memory.update_agent_inbox_item(int(item_id), status=status)
    updated = memory.get_agent_inbox_item(int(item_id)) or item
    memory.add_agent_event(
        event_type=f"inbox_item_{status}",
        source="agent_inbox",
        source_id=str(item_id),
        payload={
            "inbox_item_id": int(item_id),
            "opportunity_id": item.get("opportunity_id"),
            "status": status,
            "reason": reason,
        },
    )
    return updated


def snooze_inbox_item(
    memory: PersonaMemory,
    item_id: int,
    hours: int = 24,
) -> dict[str, Any]:
    """Snooze an inbox item until a later dashboard refresh."""
    item = memory.get_agent_inbox_item(int(item_id))
    if item is None:
        raise ValueError(f"Inbox item {item_id} does not exist")
    snoozed_until = (
        datetime.now() + timedelta(hours=max(1, int(hours)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    memory.update_agent_inbox_item(
        int(item_id),
        status="snoozed",
        snoozed_until=snoozed_until,
    )
    updated = memory.get_agent_inbox_item(int(item_id)) or item
    memory.add_agent_event(
        event_type="inbox_item_snoozed",
        source="agent_inbox",
        source_id=str(item_id),
        payload={
            "inbox_item_id": int(item_id),
            "opportunity_id": item.get("opportunity_id"),
            "snoozed_until": snoozed_until,
        },
    )
    return updated


def decode_inbox_actions(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode an inbox item's stored actions list."""
    raw = item.get("actions_json") or "[]"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _card_from_opportunity(
    store: SharedGoalStore,
    opportunity: dict[str, Any],
) -> dict[str, Any] | None:
    kind = opportunity["kind"]
    if kind == "suggest_goal_step":
        return _suggestion_card(store, opportunity)
    if kind == "accountability_check":
        return _accountability_card(store, opportunity)
    return None


def _suggestion_card(
    store: SharedGoalStore,
    opportunity: dict[str, Any],
) -> dict[str, Any] | None:
    step_id = opportunity.get("step_id")
    if step_id is None:
        return None
    step = store.get_step(int(step_id))
    if step is None or step["status"] != "suggested":
        return None
    goal = store.get_goal(step["goal_id"])
    if goal is None:
        return None

    return {
        "priority": _priority_from_opportunity(opportunity),
        "title": f"Suggested step: {step['title']}",
        "body": (
            f"For {goal['title']}. {opportunity.get('rationale') or step.get('rationale') or ''}"
        ).strip(),
        "actions": [
            _step_action("accept_step", step),
            _step_action("reject_step", step),
            _open_chat_action("step", int(step["id"])),
            {"type": "dismiss", "label": "Dismiss"},
        ],
    }


def _accountability_card(
    store: SharedGoalStore,
    opportunity: dict[str, Any],
) -> dict[str, Any] | None:
    step_id = opportunity.get("step_id")
    if step_id is None:
        return None
    step = store.get_step(int(step_id))
    if step is None or step["status"] != "accepted":
        return None
    goal = store.get_goal(step["goal_id"])
    if goal is None:
        return None
    stale_hours = _stale_hours(opportunity)

    return {
        "priority": _priority_from_opportunity(opportunity),
        "title": f"Check in: {step['title']}",
        "body": (
            f"Accepted step for {goal['title']}. "
            f"It has been open for about {stale_hours} hours."
        ),
        "actions": [
            _step_action("complete_step", step, label="Done"),
            _step_action("abandon_step", step, label="Abandon"),
            _open_chat_action("step", int(step["id"])),
            {"type": "snooze", "label": "Snooze"},
        ],
    }


def _step_action(
    action_type: str,
    step: dict[str, Any],
    label: str | None = None,
) -> dict[str, Any]:
    labels = {
        "accept_step": "Accept",
        "reject_step": "Reject",
        "complete_step": "Done",
        "abandon_step": "Abandon",
    }
    return {
        "type": action_type,
        "label": label or labels[action_type],
        "step_id": int(step["id"]),
        "goal_id": int(step["goal_id"]),
    }


def _open_chat_action(scope_type: str, scope_id: int) -> dict[str, Any]:
    return {
        "type": "open_chat",
        "label": "Open chat",
        "scope_type": scope_type,
        "scope_id": int(scope_id),
    }


def _priority_from_opportunity(opportunity: dict[str, Any]) -> int:
    urgency = int(opportunity.get("urgency") or 0)
    impact = int(opportunity.get("impact") or 0)
    attention_cost = int(opportunity.get("attention_cost") or 0)
    return max(1, min(5, urgency + impact - attention_cost))


def _stale_hours(opportunity: dict[str, Any]) -> int:
    raw = opportunity.get("proposed_action_json")
    if not raw:
        return 24
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return 24
    value = decoded.get("stale_hours") if isinstance(decoded, dict) else None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 24
