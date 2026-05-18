"""Deterministic reflection over recent agent events."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from goals import SharedGoalStore
from memory import PersonaMemory, REFLECTION_EVENT_TYPES


STEP_OUTCOME_EXPIRY_DAYS = 90
FEEDBACK_EXPIRY_DAYS = 30
CORRECTION_EXPIRY_DAYS = 14
IGNORED_PATTERN_EXPIRY_DAYS = 30
CORRECTION_MARKERS = (
    "actually",
    "do not",
    "don't",
    "instead",
    "no,",
    "no ",
    "not ",
    "please don't",
    "please stop",
    "stop ",
    "that's wrong",
    "that is wrong",
    "you should",
)


def run_reflection_job(
    memory: PersonaMemory,
    store: SharedGoalStore | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """
    Process recent reflectable events into typed durable memory.

    Phase F keeps this path deterministic and idempotent: the processor reads
    unprocessed event rows, records evidence-backed memory items, then marks
    those events processed so future runs only handle new evidence.
    """
    store = store or SharedGoalStore()
    events = memory.get_unprocessed_agent_events(
        event_types=REFLECTION_EVENT_TYPES,
        limit=limit,
    )
    if not events:
        return {
            "processed_event_ids": [],
            "memory_item_ids": [],
            "summary": "No new reflectable events.",
        }

    memory_item_ids: list[int] = []
    processed_event_ids: list[int] = []

    for event in events:
        payload = _decode_payload(event.get("payload_json"))
        new_ids = _memory_from_event(
            memory=memory,
            store=store,
            event=event,
            payload=payload,
        )
        memory_item_ids.extend(new_ids)
        processed_event_ids.append(int(event["id"]))

    marked = memory.mark_agent_events_processed(processed_event_ids)
    summary = {
        "processed_event_ids": processed_event_ids,
        "memory_item_ids": sorted(set(memory_item_ids)),
        "summary": (
            f"Processed {marked} events into "
            f"{len(set(memory_item_ids))} memory updates."
        ),
    }
    if processed_event_ids:
        memory.add_agent_event(
            event_type="reflection_cycle_completed",
            source="reflection",
            payload={
                "processed_event_ids": processed_event_ids,
                "memory_item_ids": sorted(set(memory_item_ids)),
            },
            schedule_reflection=False,
        )
    return summary


def _memory_from_event(
    memory: PersonaMemory,
    store: SharedGoalStore,
    event: dict[str, Any],
    payload: dict[str, Any],
) -> list[int]:
    event_type = event["event_type"]
    if event_type == "conversation_message":
        return _reflect_conversation_message(memory, event, payload)
    if event_type.startswith("step_"):
        return _reflect_step_event(memory, store, event, payload)
    if event_type in {"inbox_item_dismissed", "inbox_item_snoozed", "inbox_item_acted"}:
        return _reflect_inbox_event(memory, event, payload)
    return []


def _reflect_conversation_message(
    memory: PersonaMemory,
    event: dict[str, Any],
    payload: dict[str, Any],
) -> list[int]:
    if payload.get("role") != "user":
        return []
    content = (payload.get("content") or "").strip()
    if not _looks_like_correction(content):
        return []
    message_id = int(payload.get("message_id") or event["source_id"] or 0)
    scope_type = payload.get("scope_type") or "default"
    scope_id = payload.get("scope_id")
    scope_label = scope_type if scope_id is None else f"{scope_type}:{scope_id}"
    item_id = memory.record_memory_item(
        kind="preference",
        subject=f"correction:message:{message_id}",
        content=(
            f"In {scope_label}, Zach corrected or redirected the assistant: "
            f"{_quoted_excerpt(content)}"
        ),
        confidence=2,
        evidence_event_ids=[int(event["id"])],
        expires_at=_expires_in_days(CORRECTION_EXPIRY_DAYS),
        source="reflection",
    )
    return [item_id]


def _reflect_step_event(
    memory: PersonaMemory,
    store: SharedGoalStore,
    event: dict[str, Any],
    payload: dict[str, Any],
) -> list[int]:
    status = event["event_type"].removeprefix("step_")
    step_id = int(payload.get("step_id") or event["source_id"] or 0)
    goal_id = int(payload.get("goal_id") or 0)
    title = (payload.get("title") or f"step #{step_id}").strip()
    goal_title = _goal_title(store, goal_id)
    evidence = [int(event["id"])]
    memory_item_ids: list[int] = []
    commitment_subject = f"step:{step_id}:commitment"

    if status == "accepted":
        memory_item_ids.append(
            memory.record_memory_item(
                kind="commitment",
                subject=commitment_subject,
                content=(
                    f"Zach accepted the step '{title}' for goal '{goal_title}'."
                ),
                confidence=4,
                evidence_event_ids=evidence,
                source="reflection",
                supersede_existing=True,
            )
        )
        return memory_item_ids

    if status in {"completed", "abandoned"}:
        commitment = memory.get_active_memory_item("commitment", commitment_subject)
        if commitment is not None:
            memory.update_memory_item_status(
                int(commitment["id"]),
                "superseded",
                evidence_event_ids=evidence,
            )
            memory_item_ids.append(int(commitment["id"]))
        verb = "completed" if status == "completed" else "abandoned"
        memory_item_ids.append(
            memory.record_memory_item(
                kind="fact",
                subject=f"step:{step_id}:outcome",
                content=(
                    f"Zach {verb} the step '{title}' for goal '{goal_title}'."
                ),
                confidence=4,
                evidence_event_ids=evidence,
                expires_at=_expires_in_days(STEP_OUTCOME_EXPIRY_DAYS),
                source="reflection",
                supersede_existing=True,
            )
        )
        return memory_item_ids

    if status == "rejected":
        memory_item_ids.append(
            memory.record_memory_item(
                kind="preference",
                subject=f"step:{step_id}:feedback",
                content=(
                    f"Zach rejected the suggested step '{title}' for goal "
                    f"'{goal_title}'. Avoid resurfacing it without new evidence."
                ),
                confidence=3,
                evidence_event_ids=evidence,
                expires_at=_expires_in_days(FEEDBACK_EXPIRY_DAYS),
                source="reflection",
                supersede_existing=True,
            )
        )
    return memory_item_ids


def _reflect_inbox_event(
    memory: PersonaMemory,
    event: dict[str, Any],
    payload: dict[str, Any],
) -> list[int]:
    event_type = event["event_type"]
    if event_type == "inbox_item_acted":
        return []

    item_id = int(payload.get("inbox_item_id") or event["source_id"] or 0)
    item = memory.get_agent_inbox_item(item_id)
    if item is None:
        return []
    opportunity = None
    if item.get("opportunity_id") is not None:
        opportunity = memory.get_agent_opportunity(int(item["opportunity_id"]))
    title = item.get("title") or f"inbox item #{item_id}"
    action_word = "dismissed" if event_type == "inbox_item_dismissed" else "snoozed"
    evidence = [int(event["id"])]
    memory_item_ids = [
        memory.record_memory_item(
            kind="preference",
            subject=f"inbox:{item_id}:feedback",
            content=(
                f"Zach {action_word} the dashboard card '{title}'. Similar cards "
                f"should clear a higher usefulness bar."
            ),
            confidence=2,
            evidence_event_ids=evidence,
            expires_at=_expires_in_days(CORRECTION_EXPIRY_DAYS),
            source="reflection",
        )
    ]
    pattern_item_id = _maybe_record_ignored_pattern(
        memory=memory,
        opportunity=opportunity,
        current_event_id=int(event["id"]),
    )
    if pattern_item_id is not None:
        memory_item_ids.append(pattern_item_id)
    return memory_item_ids


def _maybe_record_ignored_pattern(
    memory: PersonaMemory,
    opportunity: dict[str, Any] | None,
    current_event_id: int,
) -> int | None:
    if opportunity is None:
        return None
    kind = opportunity.get("kind")
    if not kind:
        return None
    recent_ids = _recent_ignored_event_ids(memory, str(kind))
    if current_event_id not in recent_ids:
        recent_ids.insert(0, current_event_id)
    recent_ids = sorted(set(recent_ids))[-3:]
    if len(recent_ids) < 2:
        return None
    return memory.record_memory_item(
        kind="preference",
        subject=f"pattern:ignored_inbox:{kind}",
        content=(
            f"Zach has repeatedly dismissed or snoozed {kind.replace('_', ' ')} "
            f"dashboard cards. Prefer fewer cards or stronger evidence before "
            f"surfacing them."
        ),
        confidence=3 if len(recent_ids) < 3 else 4,
        evidence_event_ids=recent_ids,
        expires_at=_expires_in_days(IGNORED_PATTERN_EXPIRY_DAYS),
        source="reflection",
        supersede_existing=True,
    )


def _recent_ignored_event_ids(memory: PersonaMemory, opportunity_kind: str) -> list[int]:
    event_ids: list[int] = []
    for event_type in ("inbox_item_dismissed", "inbox_item_snoozed"):
        for event in memory.get_agent_events(event_type=event_type, source="agent_inbox", limit=50):
            payload = _decode_payload(event.get("payload_json"))
            item_id = payload.get("inbox_item_id")
            if item_id is None:
                continue
            item = memory.get_agent_inbox_item(int(item_id))
            if item is None or item.get("opportunity_id") is None:
                continue
            opportunity = memory.get_agent_opportunity(int(item["opportunity_id"]))
            if opportunity is None or opportunity.get("kind") != opportunity_kind:
                continue
            event_ids.append(int(event["id"]))
    return sorted(set(event_ids))


def _goal_title(store: SharedGoalStore, goal_id: int) -> str:
    goal = store.get_goal(goal_id)
    if goal is None:
        return f"goal #{goal_id}"
    return goal["title"]


def _decode_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _looks_like_correction(content: str) -> bool:
    lowered = content.casefold()
    if len(lowered) < 6:
        return False
    return any(marker in lowered for marker in CORRECTION_MARKERS)


def _quoted_excerpt(content: str, limit: int = 180) -> str:
    excerpt = " ".join(content.split())
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 3].rstrip() + "..."
    return f'"{excerpt}"'


def _expires_in_days(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
