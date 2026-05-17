"""Dashboard routes for database-backed Goals dashboard rendering."""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

import brain
import memory as memory_module
import personas
from agent import strip_schedule_updates
from context import DEVICE_TERMINAL, assemble_context
from dashboard.motivation import title_for_date
from goals import SharedGoalStore
from memory import MessageScope, PersonaMemory
from summarizer import check_and_summarize


TEMPLATE_DIR = Path(__file__).parent / "templates"
DASHBOARD_PERSONA = "jo"
CHAT_MESSAGE_PAGE_SIZE = 20

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@dataclass(frozen=True)
class ChatStreamJob:
    scope: MessageScope


_STREAM_JOBS: dict[str, ChatStreamJob] = {}


def category_class(category: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "-"
        for character in category.strip().lower()
    )
    return f"category-{cleaned.strip('-') or 'general'}"


def get_store() -> SharedGoalStore:
    db_path = os.environ.get("PURCIVAL_GOALS_DB")
    return SharedGoalStore(Path(db_path)) if db_path else SharedGoalStore()


def get_dashboard_persona() -> str:
    return os.environ.get("PURCIVAL_DASHBOARD_PERSONA", DASHBOARD_PERSONA)


def get_memory() -> PersonaMemory:
    memory_data_dir = os.environ.get("PURCIVAL_MEMORY_DATA_DIR")
    if memory_data_dir:
        memory_module.DATA_DIR = Path(memory_data_dir)
    return PersonaMemory(get_dashboard_persona())


def get_provider() -> str | None:
    return os.environ.get("PURCIVAL_DASHBOARD_PROVIDER")


def build_dashboard_model(store: SharedGoalStore) -> dict[str, Any]:
    goals = store.list_goals(status="active")
    steps = store.list_steps()
    goals_by_id = {goal["id"]: goal for goal in goals}

    goal_cards = []
    for goal in goals:
        goal_cards.append({
            **goal,
            "category_class": category_class(goal["category"]),
            "scope_type": "goal",
            "scope_id": goal["id"],
        })

    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for goal in goal_cards:
        categories[goal["category"]].append(goal)

    suggestions = build_step_cards(steps, goals_by_id, "suggested")
    accepted_steps = build_step_cards(steps, goals_by_id, "accepted")

    active_context = suggestions[0] if suggestions else accepted_steps[0] if accepted_steps else None
    chat_context = chat_context_from_step(active_context) if active_context else None

    return {
        "categories": dict(categories),
        "goals": goal_cards,
        "suggestions": suggestions,
        "accepted_steps": accepted_steps,
        "visible_steps": suggestions + accepted_steps,
        "active_context": active_context,
        "chat_context": chat_context,
        "chat_messages": [],
        "has_more_messages": False,
        "initial_title": title_for_date(),
    }


def build_step_cards(
    steps: list[dict[str, Any]],
    goals_by_id: dict[int, dict[str, Any]],
    status: str,
) -> list[dict[str, Any]]:
    cards = []
    for step in steps:
        if step["status"] != status:
            continue
        goal = goals_by_id.get(step["goal_id"])
        if goal is None:
            continue
        cards.append({
            **step,
            "goal": goal,
            "category": goal["category"],
            "category_class": category_class(goal["category"]),
            "display_text": step["title"],
            "scope_type": "step",
            "scope_id": step["id"],
        })
    return cards


def chat_context_from_step(step: dict[str, Any] | None) -> dict[str, Any] | None:
    if step is None:
        return None
    goal = step["goal"]
    return {
        "scope_type": "step",
        "scope_id": step["id"],
        "category_class": step["category_class"],
        "tag": f"{goal['category']} / step",
        "heading": goal["title"],
        "body": step["title"],
    }


def chat_context_from_scope(
    store: SharedGoalStore,
    scope: MessageScope,
) -> dict[str, Any]:
    if scope.scope_type == "goal":
        goal = _require_goal(store, scope.scope_id)
        return {
            "scope_type": "goal",
            "scope_id": goal["id"],
            "category_class": category_class(goal["category"]),
            "tag": f"{goal['category']} / goal",
            "heading": goal["title"],
            "body": goal.get("description") or "Goal thread",
        }

    step = _require_step(store, scope.scope_id)
    goal = _require_goal(store, step["goal_id"])
    return {
        "scope_type": "step",
        "scope_id": step["id"],
        "category_class": category_class(goal["category"]),
        "tag": f"{goal['category']} / step",
        "heading": goal["title"],
        "body": step["title"],
    }


def build_chat_panel_model(
    request: Request,
    store: SharedGoalStore,
    scope: MessageScope,
) -> dict[str, Any]:
    memory = get_memory()
    chat_messages = memory.get_recent_messages(
        limit=CHAT_MESSAGE_PAGE_SIZE,
        scope=scope,
    )
    total_messages = memory.get_message_count(scope=scope)
    return {
        "request": request,
        "chat_context": chat_context_from_scope(store, scope),
        "chat_messages": chat_messages,
        "has_more_messages": total_messages > len(chat_messages),
    }


def parse_chat_scope(
    store: SharedGoalStore,
    scope_type: str,
    scope_id: int,
) -> MessageScope:
    if scope_type == "goal":
        _require_goal(store, scope_id)
        return MessageScope.goal(scope_id)
    if scope_type == "step":
        _require_step(store, scope_id)
        return MessageScope.step(scope_id)
    raise HTTPException(status_code=404, detail="Unknown chat scope")


def _require_goal(store: SharedGoalStore, goal_id: int | None) -> dict[str, Any]:
    if goal_id is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal = store.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return goal


def _require_step(store: SharedGoalStore, step_id: int | None) -> dict[str, Any]:
    if step_id is None:
        raise HTTPException(status_code=404, detail="Step not found")
    step = store.get_step(step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    return step


def build_entity_context(store: SharedGoalStore, scope: MessageScope) -> str:
    if scope.scope_type == "goal":
        goal = _require_goal(store, scope.scope_id)
        steps = store.list_steps(goal_id=goal["id"])
        lines = [
            "You are chatting with Zach about this goal.",
            f"Goal: {goal['title']}",
            f"Category: {goal['category']}",
            f"Status: {goal['status']}",
        ]
        if goal.get("description"):
            lines.append(f"Description: {goal['description']}")
        lines.append("")
        lines.append("Steps:")
        if steps:
            for step in steps:
                lines.append(f"- [{step['status']}] {step['title']}")
        else:
            lines.append("- None yet.")
        return "\n".join(lines)

    step = _require_step(store, scope.scope_id)
    goal = _require_goal(store, step["goal_id"])
    sibling_steps = [
        sibling for sibling in store.list_steps(goal_id=goal["id"])
        if sibling["id"] != step["id"]
    ]
    feedback = store.list_step_feedback(step["id"])

    lines = [
        "You are chatting with Zach about this step.",
        f"Goal: {goal['title']}",
        f"Category: {goal['category']}",
        f"Step: {step['title']}",
        f"Status: {step['status']}",
    ]
    if step.get("description"):
        lines.append(f"Description: {step['description']}")
    if step.get("rationale"):
        lines.append(f"Rationale: {step['rationale']}")
    if sibling_steps:
        lines.append("")
        lines.append("Sibling steps:")
        for sibling in sibling_steps[:8]:
            lines.append(f"- [{sibling['status']}] {sibling['title']}")
    if feedback:
        lines.append("")
        lines.append("Recent feedback:")
        for row in feedback[-5:]:
            value = row.get("value") or ""
            lines.append(f"- {row['kind']}: {value}")
    return "\n".join(lines)


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def stream_chat_response(stream_id: str):
    job = _STREAM_JOBS.pop(stream_id, None)
    if job is None:
        yield format_sse("error", {"message": "Chat stream not found"})
        return

    memory = get_memory()
    store = get_store()

    try:
        persona_prompt = personas.load_persona(get_dashboard_persona())
        entity_context = build_entity_context(store, job.scope)
        system_prompt, messages = assemble_context(
            persona_prompt,
            memory,
            device=DEVICE_TERMINAL,
            scope=job.scope,
            entity_context=entity_context,
        )
        fake_response = os.environ.get("PURCIVAL_DASHBOARD_FAKE_RESPONSE")
        response_source = (
            [fake_response]
            if fake_response is not None
            else brain.stream(
                messages,
                system=system_prompt,
                provider=get_provider(),
                task="chat",
            )
        )
        chunks = []
        for chunk in response_source:
            if not chunk:
                continue
            chunks.append(chunk)
            yield format_sse("delta", {"text": chunk})

        response_text = "".join(chunks)
        clean_response, _actions_json = strip_schedule_updates(response_text)
        assistant_id = memory.add_message("assistant", clean_response, scope=job.scope)

        try:
            check_and_summarize(memory, scope=job.scope)
        except Exception:
            pass

        yield format_sse("done", {"message_id": assistant_id})
    except Exception as exc:
        yield format_sse("error", {"message": str(exc)})


def render_suggestion_strip(request: Request):
    model = build_dashboard_model(get_store())
    return templates.TemplateResponse(
        request,
        "partials/suggestion_strip.html",
        model,
    )


@router.get("/")
def index(request: Request):
    model = build_dashboard_model(get_store())
    return templates.TemplateResponse(
        request,
        "index.html",
        model,
    )


@router.get("/partials/goals")
def goal_strip(request: Request):
    model = build_dashboard_model(get_store())
    return templates.TemplateResponse(
        request,
        "partials/goal_strip.html",
        model,
    )


@router.get("/partials/suggestions")
def suggestion_strip(request: Request):
    return render_suggestion_strip(request)


@router.get("/partials/chat")
def chat_panel(
    request: Request,
    scope_type: str | None = None,
    scope_id: int | None = None,
):
    store = get_store()
    if scope_type is not None and scope_id is not None:
        scope = parse_chat_scope(store, scope_type, scope_id)
        model = build_chat_panel_model(request, store, scope)
    else:
        model = build_dashboard_model(store)
    return templates.TemplateResponse(
        request,
        "partials/chat_panel.html",
        model,
    )


@router.get("/chat/streams/{stream_id}")
def chat_stream(stream_id: str):
    return StreamingResponse(
        stream_chat_response(stream_id),
        media_type="text/event-stream",
    )


@router.get("/chat/{scope_type}/{scope_id}")
def scoped_chat_panel(scope_type: str, scope_id: int, request: Request):
    store = get_store()
    scope = parse_chat_scope(store, scope_type, scope_id)
    model = build_chat_panel_model(request, store, scope)
    return templates.TemplateResponse(
        request,
        "partials/chat_panel.html",
        model,
    )


@router.get("/chat/{scope_type}/{scope_id}/messages")
def scoped_chat_messages(
    scope_type: str,
    scope_id: int,
    before_id: int | None = None,
    limit: int = CHAT_MESSAGE_PAGE_SIZE,
):
    store = get_store()
    scope = parse_chat_scope(store, scope_type, scope_id)
    page_size = min(max(limit, 1), 50)
    memory = get_memory()
    if before_id is None:
        messages = memory.get_recent_messages(limit=page_size, scope=scope)
        return {
            "messages": messages,
            "has_more": memory.get_message_count(scope=scope) > len(messages),
        }

    older_messages = memory.get_messages_before(
        before_id=before_id,
        limit=page_size + 1,
        scope=scope,
    )
    has_more = len(older_messages) > page_size
    if has_more:
        older_messages = older_messages[1:]
    return {"messages": older_messages, "has_more": has_more}


@router.post("/chat/{scope_type}/{scope_id}/messages")
def create_chat_message(
    scope_type: str,
    scope_id: int,
    message: str = Form(...),
):
    cleaned = message.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    store = get_store()
    scope = parse_chat_scope(store, scope_type, scope_id)
    user_message_id = get_memory().add_message("user", cleaned, scope=scope)

    stream_id = uuid.uuid4().hex
    _STREAM_JOBS[stream_id] = ChatStreamJob(scope=scope)
    return {"stream_id": stream_id, "message_id": user_message_id}


@router.post("/steps/{step_id}/accept")
def accept_step(step_id: int, request: Request):
    try:
        if not get_store().accept_step(step_id):
            raise HTTPException(status_code=404, detail="Step not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request)


@router.post("/steps/{step_id}/reject")
def reject_step(
    step_id: int,
    request: Request,
):
    try:
        if not get_store().reject_step(step_id):
            raise HTTPException(status_code=404, detail="Step not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request)
