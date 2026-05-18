"""Accountability helpers for event-backed goal step updates."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from goals import STEP_STATUSES, SharedGoalStore
from memory import PersonaMemory


ACCOUNTABILITY_STALE_AFTER_HOURS = 24
ACCOUNTABILITY_KIND = "accountability_check"


def record_step_status_change(
    store: SharedGoalStore,
    memory: PersonaMemory | None,
    step_id: int,
    status: str,
    source: str,
    actor: str = "jo",
    note: str | None = None,
    evidence_event_ids: list[int] | None = None,
    related_message_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Update a step and write the matching event/receipt metadata."""
    if status not in STEP_STATUSES:
        choices = ", ".join(sorted(STEP_STATUSES))
        raise ValueError(f"Invalid status '{status}'. Expected one of: {choices}")

    step = store.get_step(int(step_id))
    if step is None:
        raise ValueError(f"Step {step_id} does not exist")

    previous_status = step["status"]
    changed = previous_status != status
    if changed and not store.update_step_status(int(step_id), status):
        raise ValueError(f"Step {step_id} does not exist")

    if note:
        store.add_step_feedback(int(step_id), _feedback_kind(status), note.strip())

    updated_step = store.get_step(int(step_id)) or step
    receipt = {
        "step_id": int(step_id),
        "goal_id": updated_step["goal_id"],
        "title": updated_step["title"],
        "previous_status": previous_status,
        "status": status,
        "changed": changed,
        "source": source,
        "actor": actor,
        "note": note,
        "evidence_event_ids": evidence_event_ids or [],
        "related_message_ids": related_message_ids or [],
        "reversible": True,
        "undo_status": previous_status,
    }

    if memory is not None:
        event_id = memory.add_agent_event(
            event_type=f"step_{status}",
            source=source,
            source_id=str(step_id),
            payload=receipt,
        )
        receipt["event_id"] = event_id
        _sync_step_opportunities(memory, int(step_id), status)
        if status == "accepted":
            opportunity_id = ensure_accountability_opportunity(
                memory,
                store,
                updated_step,
                source_event_id=event_id,
            )
            receipt["accountability_opportunity_id"] = opportunity_id

    return receipt


def ensure_accountability_opportunity(
    memory: PersonaMemory,
    store: SharedGoalStore,
    step: dict[str, Any],
    source_event_id: int | None = None,
) -> int:
    """Create or refresh the durable accountability opportunity for a step."""
    step_id = int(step["id"])
    duplicate_key = accountability_duplicate_key(step_id)
    score = score_accountability_step(step)
    proposed_action = {
        "tool": "steps",
        "method": "accountability_check",
        "parameters": {
            "step_id": step_id,
            "goal_id": step["goal_id"],
            "title": step["title"],
        },
    }
    existing = memory.get_agent_opportunity_by_duplicate_key(duplicate_key)
    if existing:
        if existing["status"] in {"completed", "rejected", "dismissed", "blocked"}:
            return int(existing["id"])
        opportunity_id = int(existing["id"])
        memory.update_agent_opportunity(
            opportunity_id,
            status=score["status"],
            urgency=score["urgency"],
            impact=score["impact"],
            confidence=score["confidence"],
            attention_cost=score["attention_cost"],
            proposed_action={
                **_decode_action(existing.get("proposed_action_json")),
                **proposed_action,
                "stale_hours": score["stale_hours"],
            },
        )
        if score["status"] == "queued":
            from delivery import deliver_opportunity_to_inbox

            opportunity = memory.get_agent_opportunity(opportunity_id)
            if opportunity:
                deliver_opportunity_to_inbox(memory, store, opportunity)
        return opportunity_id

    evidence = [source_event_id] if source_event_id is not None else []
    opportunity_id = memory.add_agent_opportunity(
        kind=ACCOUNTABILITY_KIND,
        title=f"Check in on accepted step: {step['title']}",
        rationale=(
            "Accepted steps should remain visible until Zach completes, "
            "abandons, or revises them."
        ),
        evidence_event_ids=evidence,
        goal_id=step["goal_id"],
        step_id=step_id,
        status=score["status"],
        urgency=score["urgency"],
        impact=score["impact"],
        confidence=score["confidence"],
        attention_cost=score["attention_cost"],
        risk_level="low",
        proposed_action={**proposed_action, "stale_hours": score["stale_hours"]},
        duplicate_key=duplicate_key,
        deliver_after=score["deliver_after"],
    )
    if score["status"] == "queued":
        from delivery import deliver_opportunity_to_inbox

        opportunity = memory.get_agent_opportunity(opportunity_id)
        if opportunity:
            deliver_opportunity_to_inbox(memory, store, opportunity)
    return opportunity_id


def refresh_accountability_opportunities(
    memory: PersonaMemory,
    store: SharedGoalStore,
) -> list[int]:
    """Ensure every accepted step has one scored accountability opportunity."""
    opportunity_ids = []
    for step in store.list_steps(status="accepted"):
        opportunity_ids.append(ensure_accountability_opportunity(memory, store, step))
    return opportunity_ids


def score_accountability_step(step: dict[str, Any]) -> dict[str, Any]:
    """Score an accepted step based on how long it has sat untouched."""
    touched_at = _parse_timestamp(
        step.get("last_touched_at")
        or step.get("accepted_at")
        or step.get("updated_at")
        or step.get("created_at")
    )
    now = datetime.now()
    stale_hours = max(0, int((now - touched_at).total_seconds() // 3600))
    stale_days = stale_hours // 24
    is_stale = stale_hours >= ACCOUNTABILITY_STALE_AFTER_HOURS
    deliver_at = touched_at + timedelta(hours=ACCOUNTABILITY_STALE_AFTER_HOURS)

    return {
        "status": "queued" if is_stale else "scheduled",
        "urgency": min(5, 1 + stale_days),
        "impact": 3,
        "confidence": 4,
        "attention_cost": 2 if is_stale else 1,
        "stale_hours": stale_hours,
        "deliver_after": deliver_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def accountability_duplicate_key(step_id: int) -> str:
    return f"{ACCOUNTABILITY_KIND}:step={int(step_id)}"


def format_receipt(receipt: dict[str, Any]) -> str:
    """Return a concise user-visible receipt for a step state change."""
    status = receipt["status"]
    verb = {
        "accepted": "accepted",
        "rejected": "rejected",
        "completed": "marked done",
        "abandoned": "abandoned",
        "suggested": "moved back to suggested",
    }.get(status, f"updated to {status}")
    previous = receipt.get("previous_status")
    title = receipt.get("title", f"step #{receipt['step_id']}")
    if previous and previous != status:
        return f"Receipt: {verb} '{title}' (was {previous})."
    return f"Receipt: '{title}' is already {status}."


def _sync_step_opportunities(
    memory: PersonaMemory,
    step_id: int,
    status: str,
):
    opportunity_status = {
        "accepted": "accepted",
        "completed": "completed",
        "rejected": "rejected",
        "abandoned": "rejected",
    }.get(status)
    if opportunity_status is None:
        return

    for opportunity in memory.list_agent_opportunities(limit=200):
        if opportunity.get("step_id") != step_id:
            continue
        if opportunity["status"] in {"dismissed", "blocked", "expired"}:
            continue
        memory.update_agent_opportunity(
            int(opportunity["id"]),
            status=opportunity_status,
        )


def _feedback_kind(status: str) -> str:
    if status == "completed":
        return "completion_note"
    if status == "abandoned":
        return "abandon_reason"
    return "freeform_note"


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.now()


def _decode_action(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
