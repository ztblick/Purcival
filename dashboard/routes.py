"""Dashboard routes for database-backed Goals dashboard rendering."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import brain
import memory as memory_module
import personas
from accountability import format_receipt, record_step_status_change
from agent import _parse_actions_json, strip_schedule_updates
from context import DEVICE_TERMINAL, assemble_context
from dashboard.auth import (
    LOGIN_LIMITER,
    clear_session_cookie,
    client_host,
    create_signed_session,
    sanitize_next_path,
    set_session_cookie,
    verify_dashboard_password,
)
from dashboard.config import DashboardConfig
from dashboard.motivation import title_for_date
from delivery import decode_inbox_actions, mark_inbox_item, snooze_inbox_item
from goals import SharedGoalStore
from memory import MessageScope, PersonaMemory
from summarizer import check_and_summarize


TEMPLATE_DIR = Path(__file__).parent / "templates"
CHAT_MESSAGE_PAGE_SIZE = 20
SCHEDULE_UPDATES_START = "<schedule_updates>"
SCHEDULE_UPDATES_END = "</schedule_updates>"
INTERNAL_ACTIONS_START = "<internal_actions>"
INTERNAL_ACTIONS_END = "</internal_actions>"
CONTROL_BLOCK_MARKERS = (
    (SCHEDULE_UPDATES_START, SCHEDULE_UPDATES_END),
    (INTERNAL_ACTIONS_START, INTERNAL_ACTIONS_END),
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@dataclass(frozen=True)
class ChatStreamJob:
    scope: MessageScope
    actor: str
    actor_metadata: dict[str, Any]
    user_message_id: int | None = None


_STREAM_JOBS: dict[str, ChatStreamJob] = {}


@dataclass(frozen=True)
class DashboardFilters:
    selected_category: str | None = None
    selected_goal_id: int | None = None


def category_class(category: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "-"
        for character in category.strip().lower()
    )
    return f"category-{cleaned.strip('-') or 'general'}"


def get_dashboard_config(request: Request | None = None) -> DashboardConfig:
    if request is None:
        from dashboard.config import load_dashboard_config

        return load_dashboard_config()
    return request.app.state.dashboard_config


def get_store(request: Request | None = None) -> SharedGoalStore:
    config = get_dashboard_config(request)
    return SharedGoalStore(config.goals_db) if config.goals_db else SharedGoalStore()


def get_dashboard_persona(request: Request | None = None) -> str:
    return get_dashboard_config(request).persona


def get_memory(request: Request | None = None) -> PersonaMemory:
    memory_data_dir = get_dashboard_config(request).memory_data_dir
    if memory_data_dir:
        memory_module.DATA_DIR = Path(memory_data_dir)
    return PersonaMemory(get_dashboard_persona(request))


def get_provider(request: Request | None = None) -> str | None:
    return get_dashboard_config(request).provider


def get_dashboard_actor(request: Request) -> str:
    return request.state.dashboard_actor


def get_dashboard_actor_metadata(request: Request) -> dict[str, Any]:
    return {"client_host": client_host(request)}


def template_context(request: Request, **model: Any) -> dict[str, Any]:
    return {
        "request": request,
        "csrf_token": getattr(request.state, "dashboard_csrf_token", ""),
        "dashboard_authenticated": getattr(request.state, "dashboard_authenticated", False),
        "dashboard_actor": getattr(request.state, "dashboard_actor", None),
        **model,
    }


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dashboard_filter_url(
    category: str | None = None,
    goal_id: int | None = None,
    path: str = "/",
) -> str:
    params: dict[str, str | int] = {}
    if category:
        params["category"] = category
    if goal_id is not None:
        params["goal_id"] = goal_id
    query = urlencode(params)
    return f"{path}?{query}" if query else path


def _filter_query_suffix(filters: DashboardFilters) -> str:
    query = _dashboard_filter_url(
        filters.selected_category,
        filters.selected_goal_id,
        path="",
    )
    return query


def _parse_dashboard_filters(
    request: Request | None,
    goals_by_id: dict[int, dict[str, Any]],
) -> DashboardFilters:
    if request is None:
        return DashboardFilters()

    active_categories = {goal["category"] for goal in goals_by_id.values()}
    raw_category = (request.query_params.get("category") or "").strip()
    selected_category = raw_category if raw_category in active_categories else None
    selected_goal_id = _optional_int(request.query_params.get("goal_id"))

    if selected_goal_id is None:
        return DashboardFilters(selected_category=selected_category)

    selected_goal = goals_by_id.get(selected_goal_id)
    if selected_goal is None:
        return DashboardFilters(selected_category=selected_category)
    if selected_category is not None and selected_goal["category"] != selected_category:
        return DashboardFilters(selected_category=selected_category)

    return DashboardFilters(
        selected_category=selected_category or selected_goal["category"],
        selected_goal_id=selected_goal_id,
    )


def _goal_matches_filter(
    goal: dict[str, Any],
    filters: DashboardFilters,
) -> bool:
    if filters.selected_category and goal["category"] != filters.selected_category:
        return False
    if filters.selected_goal_id is not None and goal["id"] != filters.selected_goal_id:
        return False
    return True


def build_dashboard_model(
    store: SharedGoalStore,
    request: Request | None = None,
) -> dict[str, Any]:
    memory = get_memory(request)
    goals = store.list_goals(status="active")
    steps = store.list_steps()
    goals_by_id = {goal["id"]: goal for goal in goals}
    filters = _parse_dashboard_filters(request, goals_by_id)

    goal_cards = []
    for goal in goals:
        goal_cards.append({
            **goal,
            "category_class": category_class(goal["category"]),
            "scope_type": "goal",
            "scope_id": goal["id"],
            "filter_url": _dashboard_filter_url(goal["category"], goal["id"]),
            "is_selected": goal["id"] == filters.selected_goal_id,
        })

    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for goal in goal_cards:
        if not _goal_matches_filter(goal, DashboardFilters(filters.selected_category)):
            continue
        categories[goal["category"]].append(goal)

    suggestions = build_step_cards(steps, goals_by_id, "suggested", filters)
    accepted_steps = build_step_cards(steps, goals_by_id, "accepted", filters)

    active_context = suggestions[0] if suggestions else accepted_steps[0] if accepted_steps else None
    chat_context = chat_context_from_step(active_context) if active_context else None
    filter_query_suffix = _filter_query_suffix(filters)

    return {
        "categories": dict(categories),
        "category_filters": build_category_filters(goals, filters),
        "goals": goal_cards,
        "suggestions": suggestions,
        "accepted_steps": accepted_steps,
        "visible_steps": suggestions + accepted_steps,
        "inbox_items": build_inbox_cards(memory, store, goals_by_id, filters),
        "active_context": active_context,
        "chat_context": chat_context,
        "chat_messages": [],
        "has_more_messages": False,
        "activity_receipt": None,
        "initial_title": title_for_date(),
        "selected_category": filters.selected_category,
        "selected_goal_id": filters.selected_goal_id,
        "filter_query_suffix": filter_query_suffix,
    }


def build_category_filters(
    goals: list[dict[str, Any]],
    filters: DashboardFilters,
) -> list[dict[str, Any]]:
    categories = sorted({goal["category"] for goal in goals})
    items = [{
        "label": "All",
        "value": "",
        "category_class": "category-general",
        "url": _dashboard_filter_url(),
        "is_active": filters.selected_category is None and filters.selected_goal_id is None,
    }]
    for category in categories:
        items.append({
            "label": category,
            "value": category,
            "category_class": category_class(category),
            "url": _dashboard_filter_url(category),
            "is_active": category == filters.selected_category,
        })
    return items


def build_inbox_cards(
    memory: PersonaMemory,
    store: SharedGoalStore,
    goals_by_id: dict[int, dict[str, Any]],
    filters: DashboardFilters,
) -> list[dict[str, Any]]:
    cards = []
    for item in memory.list_agent_inbox_items(status="unread", surface="dashboard"):
        actions = decode_inbox_actions(item)
        if not _inbox_item_matches_filter(actions, store, goals_by_id, filters):
            continue
        action_types = {action.get("type") for action in actions}
        open_chat = next(
            (action for action in actions if action.get("type") == "open_chat"),
            None,
        )
        cards.append({
            **item,
            "actions": actions,
            "action_types": action_types,
            "open_chat": open_chat,
            "priority_label": f"P{item['priority']}",
        })
    return cards


def _inbox_item_matches_filter(
    actions: list[dict[str, Any]],
    store: SharedGoalStore,
    goals_by_id: dict[int, dict[str, Any]],
    filters: DashboardFilters,
) -> bool:
    step_id = _inbox_step_id(actions)
    if step_id is None:
        return True
    step = store.get_step(step_id)
    if step is None:
        return False
    goal = goals_by_id.get(step["goal_id"])
    if goal is None:
        return False
    return _goal_matches_filter(goal, filters)


def _inbox_step_id(actions: list[dict[str, Any]]) -> int | None:
    for action in actions:
        step_id = action.get("step_id")
        if step_id is not None:
            return _optional_int(str(step_id))
    for action in actions:
        if action.get("type") == "open_chat" and action.get("scope_type") == "step":
            return _optional_int(str(action.get("scope_id")))
    return None


def build_step_cards(
    steps: list[dict[str, Any]],
    goals_by_id: dict[int, dict[str, Any]],
    status: str,
    filters: DashboardFilters = DashboardFilters(),
) -> list[dict[str, Any]]:
    cards = []
    for step in steps:
        if step["status"] != status:
            continue
        goal = goals_by_id.get(step["goal_id"])
        if goal is None:
            continue
        if not _goal_matches_filter(goal, filters):
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
    memory = get_memory(request)
    chat_messages = memory.get_recent_messages(
        limit=CHAT_MESSAGE_PAGE_SIZE,
        scope=scope,
    )
    total_messages = memory.get_message_count(scope=scope)
    return template_context(
        request,
        chat_context=chat_context_from_scope(store, scope),
        chat_messages=chat_messages,
        has_more_messages=total_messages > len(chat_messages),
    )


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
        "",
        "Trusted internal updates:",
        (
            "If the conversation gives clear evidence that this step should "
            "be completed or abandoned, append an internal action block after "
            "your normal reply. The dashboard will hide the block, apply the "
            "update, record an event, and show Zach a receipt."
        ),
        (
            f"Use only this current step id: {step['id']}. Format exactly: "
            '<internal_actions>[{"tool":"steps","method":"complete_step",'
            '"parameters":{"step_id":'
            f'{step["id"]},"note":"short evidence"}}]</internal_actions>'
        ),
        (
            "Use method abandon_step instead when the step is no longer "
            "relevant. Do not use this block for external actions."
        ),
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


def extract_internal_actions(response: str) -> str | None:
    start_index = response.find(INTERNAL_ACTIONS_START)
    if start_index == -1:
        return None
    content_start = start_index + len(INTERNAL_ACTIONS_START)
    end_index = response.find(INTERNAL_ACTIONS_END, content_start)
    if end_index == -1:
        return None
    return response[content_start:end_index].strip()


def strip_internal_actions(response: str) -> str:
    cleaned = response
    while True:
        start_index = cleaned.find(INTERNAL_ACTIONS_START)
        if start_index == -1:
            return cleaned.strip()
        end_index = cleaned.find(
            INTERNAL_ACTIONS_END,
            start_index + len(INTERNAL_ACTIONS_START),
        )
        if end_index == -1:
            return cleaned[:start_index].strip()
        cleaned = (
            cleaned[:start_index]
            + cleaned[end_index + len(INTERNAL_ACTIONS_END):]
        )


def apply_chat_internal_actions(
    actions_json: str | None,
    store: SharedGoalStore,
    memory: PersonaMemory,
    scope: MessageScope,
    actor: str,
    actor_metadata: dict[str, Any],
    user_message_id: int | None,
    assistant_message_id: int | None,
) -> list[dict[str, Any]]:
    if not actions_json:
        return []

    parsed_actions, parse_error = _parse_actions_json(actions_json)
    if parse_error:
        return [{
            "status": "failed",
            "message": f"Receipt failed: {parse_error}",
        }]

    receipts = []
    for action in parsed_actions:
        try:
            receipt = _apply_one_chat_internal_action(
                action,
                store,
                memory,
                scope,
                actor,
                actor_metadata,
                user_message_id,
                assistant_message_id,
            )
            receipts.append(receipt)
        except ValueError as exc:
            receipts.append({
                "status": "failed",
                "message": f"Receipt failed: {exc}",
            })
    return receipts


def _apply_one_chat_internal_action(
    action: dict[str, Any],
    store: SharedGoalStore,
    memory: PersonaMemory,
    scope: MessageScope,
    actor: str,
    actor_metadata: dict[str, Any],
    user_message_id: int | None,
    assistant_message_id: int | None,
) -> dict[str, Any]:
    tool_name = action.get("tool")
    method_name = action.get("method")
    parameters = action.get("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}
    if tool_name not in {"steps", "suggestions"}:
        raise ValueError("chat internal actions may only update steps")
    status_by_method = {
        "complete_step": "completed",
        "abandon_step": "abandoned",
        "update_status": parameters.get("status"),
    }
    status = status_by_method.get(method_name)
    if status not in {"completed", "abandoned"}:
        raise ValueError("chat internal actions may only complete or abandon steps")

    step_id = int(parameters.get("step_id") or 0)
    if scope.scope_type != "step" or step_id != scope.scope_id:
        raise ValueError("chat internal actions may only update the active step")

    note = parameters.get("note")
    return record_step_status_change(
        store=store,
        memory=memory,
        step_id=step_id,
        status=status,
        source="dashboard_chat",
        actor=actor,
        actor_metadata=actor_metadata,
        note=note if isinstance(note, str) else None,
        related_message_ids=[
            message_id
            for message_id in (user_message_id, assistant_message_id)
            if message_id is not None
        ],
    )


def _matching_marker_suffix_length(text: str, marker: str) -> int:
    max_length = min(len(text), len(marker) - 1)
    for length in range(max_length, 0, -1):
        if marker.startswith(text[-length:]):
            return length
    return 0


def _split_visible_prefix(buffer: str) -> tuple[str, str]:
    keep_length = max(
        _matching_marker_suffix_length(buffer, start_marker)
        for start_marker, _end_marker in CONTROL_BLOCK_MARKERS
    )
    if keep_length == 0:
        return buffer, ""
    return buffer[:-keep_length], buffer[-keep_length:]


def _discard_until_possible_end(buffer: str, end_marker: str) -> str:
    keep_length = _matching_marker_suffix_length(buffer, end_marker)
    if keep_length == 0:
        return ""
    return buffer[-keep_length:]


def _find_first_control_start(buffer: str) -> tuple[int, str, str] | None:
    matches = [
        (buffer.find(start_marker), start_marker, end_marker)
        for start_marker, end_marker in CONTROL_BLOCK_MARKERS
        if buffer.find(start_marker) != -1
    ]
    if not matches:
        return None
    return min(matches, key=lambda match: match[0])


def iter_user_visible_chunks(chunks: Iterable[str]) -> Iterator[str]:
    """
    Stream assistant text while suppressing hidden schedule control blocks.

    The dashboard must never show machine-readable <schedule_updates> tags,
    even when a provider splits the tag across arbitrary streaming chunks.
    """
    buffer = ""
    active_end_marker: str | None = None

    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk

        while buffer:
            if active_end_marker is not None:
                end_index = buffer.find(active_end_marker)
                if end_index == -1:
                    buffer = _discard_until_possible_end(buffer, active_end_marker)
                    break
                buffer = buffer[end_index + len(active_end_marker):]
                active_end_marker = None
                continue

            control_start = _find_first_control_start(buffer)
            if control_start is not None:
                start_index, start_marker, end_marker = control_start
                visible = buffer[:start_index]
                if visible:
                    yield visible
                buffer = buffer[start_index + len(start_marker):]
                active_end_marker = end_marker
                continue

            visible, buffer = _split_visible_prefix(buffer)
            if visible:
                yield visible
            break

    if buffer and active_end_marker is None:
        yield buffer


def stream_chat_response(request: Request, stream_id: str):
    job = _STREAM_JOBS.pop(stream_id, None)
    if job is None:
        yield format_sse("error", {"message": "Chat stream not found"})
        return

    memory = get_memory(request)
    store = get_store(request)

    try:
        persona_prompt = personas.load_persona(get_dashboard_persona(request))
        entity_context = build_entity_context(store, job.scope)
        system_prompt, messages = assemble_context(
            persona_prompt,
            memory,
            device=DEVICE_TERMINAL,
            scope=job.scope,
            entity_context=entity_context,
        )
        fake_response = get_dashboard_config(request).fake_response
        response_source = (
            [fake_response]
            if fake_response is not None
            else brain.stream(
                messages,
                system=system_prompt,
                provider=get_provider(request),
                task="chat",
            )
        )
        raw_chunks = []

        def observed_chunks():
            for chunk in response_source:
                raw_chunks.append(chunk)
                yield chunk

        chunks = []
        for chunk in iter_user_visible_chunks(observed_chunks()):
            chunks.append(chunk)
            yield format_sse("delta", {"text": chunk})

        raw_response = "".join(raw_chunks)
        internal_actions_json = extract_internal_actions(raw_response)
        response_text = "".join(chunks)
        clean_response, _actions_json = strip_schedule_updates(response_text)
        clean_response = strip_internal_actions(clean_response)
        assistant_id = memory.add_message("assistant", clean_response, scope=job.scope)

        receipts = apply_chat_internal_actions(
            internal_actions_json,
            store,
            memory,
            job.scope,
            job.actor,
            job.actor_metadata,
            job.user_message_id,
            assistant_id,
        )
        for receipt in receipts:
            receipt_text = receipt.get("message") or format_receipt(receipt)
            receipt_id = memory.add_message(
                "assistant",
                receipt_text,
                scope=job.scope,
            )
            yield format_sse("receipt", {
                "text": receipt_text,
                "message_id": receipt_id,
                "step_id": receipt.get("step_id"),
                "status": receipt.get("status"),
            })

        try:
            check_and_summarize(memory, scope=job.scope)
        except Exception:
            pass

        yield format_sse("done", {"message_id": assistant_id})
    except Exception as exc:
        yield format_sse("error", {"message": str(exc)})


def render_suggestion_strip(
    request: Request,
    activity_receipt: str | None = None,
):
    model = build_dashboard_model(get_store(request), request=request)
    model["activity_receipt"] = activity_receipt
    return templates.TemplateResponse(
        request,
        "partials/suggestion_strip.html",
        template_context(request, **model),
    )


@router.get("/login")
def login_page(request: Request, next: str | None = None):
    if request.state.dashboard_authenticated:
        return RedirectResponse(url=sanitize_next_path(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        template_context(
            request,
            next_path=sanitize_next_path(next),
            error_message=None,
        ),
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next_path: str = Form("/", alias="next"),
):
    config = get_dashboard_config(request)
    host = client_host(request)
    if LOGIN_LIMITER.is_locked(
        host,
        max_failures=config.login_limit_failures,
        window_seconds=config.login_limit_window_seconds,
    ):
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(
                request,
                next_path=sanitize_next_path(next_path),
                error_message="Login unavailable for a short time. Try again soon.",
            ),
            status_code=429,
        )

    if not verify_dashboard_password(password, config.password_hash):
        LOGIN_LIMITER.register_failure(
            host,
            window_seconds=config.login_limit_window_seconds,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(
                request,
                next_path=sanitize_next_path(next_path),
                error_message="Login failed. Check the password and try again.",
            ),
            status_code=401,
        )

    LOGIN_LIMITER.clear(host)
    response = RedirectResponse(url=sanitize_next_path(next_path), status_code=303)
    token = create_signed_session(
        config.secret_key,
        session_days=config.session_days,
    )
    set_session_cookie(response, config, token)
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response, get_dashboard_config(request))
    return response


@router.get("/")
def index(request: Request):
    model = build_dashboard_model(get_store(request), request=request)
    return templates.TemplateResponse(
        request,
        "index.html",
        template_context(request, **model),
    )


@router.get("/partials/goals")
def goal_strip(request: Request):
    model = build_dashboard_model(get_store(request), request=request)
    return templates.TemplateResponse(
        request,
        "partials/goal_strip.html",
        template_context(request, **model),
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
    store = get_store(request)
    if scope_type is not None and scope_id is not None:
        scope = parse_chat_scope(store, scope_type, scope_id)
        model = build_chat_panel_model(request, store, scope)
    else:
        model = template_context(request, **build_dashboard_model(store, request=request))
    return templates.TemplateResponse(
        request,
        "partials/chat_panel.html",
        model,
    )


@router.get("/chat/streams/{stream_id}")
def chat_stream(request: Request, stream_id: str):
    return StreamingResponse(
        stream_chat_response(request, stream_id),
        media_type="text/event-stream",
    )


@router.get("/chat/{scope_type}/{scope_id}")
def scoped_chat_panel(scope_type: str, scope_id: int, request: Request):
    store = get_store(request)
    scope = parse_chat_scope(store, scope_type, scope_id)
    model = build_chat_panel_model(request, store, scope)
    return templates.TemplateResponse(
        request,
        "partials/chat_panel.html",
        model,
    )


@router.get("/chat/{scope_type}/{scope_id}/messages")
def scoped_chat_messages(
    request: Request,
    scope_type: str,
    scope_id: int,
    before_id: int | None = None,
    limit: int = CHAT_MESSAGE_PAGE_SIZE,
):
    store = get_store(request)
    scope = parse_chat_scope(store, scope_type, scope_id)
    page_size = min(max(limit, 1), 50)
    memory = get_memory(request)
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
    request: Request,
    scope_type: str,
    scope_id: int,
    message: str = Form(...),
):
    cleaned = message.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    store = get_store(request)
    scope = parse_chat_scope(store, scope_type, scope_id)
    user_message_id = get_memory(request).add_message("user", cleaned, scope=scope)

    stream_id = uuid.uuid4().hex
    _STREAM_JOBS[stream_id] = ChatStreamJob(
        scope=scope,
        actor=get_dashboard_actor(request),
        actor_metadata=get_dashboard_actor_metadata(request),
        user_message_id=user_message_id,
    )
    return {"stream_id": stream_id, "message_id": user_message_id}


def _require_inbox_item(memory: PersonaMemory, item_id: int) -> dict[str, Any]:
    item = memory.get_agent_inbox_item(int(item_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return item


def _require_inbox_step_action(
    item: dict[str, Any],
    action_type: str,
) -> int:
    for action in decode_inbox_actions(item):
        if action.get("type") == action_type:
            step_id = action.get("step_id")
            if step_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Inbox action is missing a step id",
                )
            return int(step_id)
    raise HTTPException(status_code=400, detail=f"Inbox item has no {action_type} action")


def _render_inbox_step_update(
    request: Request,
    item_id: int,
    action_type: str,
    status: str,
) -> Any:
    store = get_store(request)
    memory = get_memory(request)
    item = _require_inbox_item(memory, item_id)
    step_id = _require_inbox_step_action(item, action_type)
    try:
        receipt = record_step_status_change(
            store=store,
            memory=memory,
            step_id=step_id,
            status=status,
            source="dashboard_inbox",
            actor=get_dashboard_actor(request),
            actor_metadata=get_dashboard_actor_metadata(request),
        )
        mark_inbox_item(memory, item_id, "acted", reason=action_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request, format_receipt(receipt))


@router.post("/inbox/{item_id}/accept")
def accept_inbox_item(item_id: int, request: Request):
    return _render_inbox_step_update(request, item_id, "accept_step", "accepted")


@router.post("/inbox/{item_id}/reject")
def reject_inbox_item(item_id: int, request: Request):
    return _render_inbox_step_update(request, item_id, "reject_step", "rejected")


@router.post("/inbox/{item_id}/complete")
def complete_inbox_item(item_id: int, request: Request):
    return _render_inbox_step_update(request, item_id, "complete_step", "completed")


@router.post("/inbox/{item_id}/abandon")
def abandon_inbox_item(item_id: int, request: Request):
    return _render_inbox_step_update(request, item_id, "abandon_step", "abandoned")


@router.post("/inbox/{item_id}/dismiss")
def dismiss_inbox_item(item_id: int, request: Request):
    memory = get_memory(request)
    _require_inbox_item(memory, item_id)
    mark_inbox_item(memory, item_id, "dismissed", reason="dashboard_dismiss")
    return render_suggestion_strip(request, "Inbox item dismissed.")


@router.post("/inbox/{item_id}/snooze")
def snooze_dashboard_inbox_item(item_id: int, request: Request):
    memory = get_memory(request)
    _require_inbox_item(memory, item_id)
    snooze_inbox_item(memory, item_id, hours=24)
    return render_suggestion_strip(request, "Inbox item snoozed until tomorrow.")


@router.post("/steps/{step_id}/accept")
def accept_step(step_id: int, request: Request):
    try:
        receipt = record_step_status_change(
            store=get_store(request),
            memory=get_memory(request),
            step_id=step_id,
            status="accepted",
            source="dashboard_ui",
            actor=get_dashboard_actor(request),
            actor_metadata=get_dashboard_actor_metadata(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request, format_receipt(receipt))


@router.post("/steps/{step_id}/reject")
def reject_step(
    step_id: int,
    request: Request,
):
    try:
        receipt = record_step_status_change(
            store=get_store(request),
            memory=get_memory(request),
            step_id=step_id,
            status="rejected",
            source="dashboard_ui",
            actor=get_dashboard_actor(request),
            actor_metadata=get_dashboard_actor_metadata(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request, format_receipt(receipt))


@router.post("/steps/{step_id}/complete")
def complete_step(step_id: int, request: Request):
    try:
        receipt = record_step_status_change(
            store=get_store(request),
            memory=get_memory(request),
            step_id=step_id,
            status="completed",
            source="dashboard_ui",
            actor=get_dashboard_actor(request),
            actor_metadata=get_dashboard_actor_metadata(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request, format_receipt(receipt))


@router.post("/steps/{step_id}/abandon")
def abandon_step(step_id: int, request: Request):
    try:
        receipt = record_step_status_change(
            store=get_store(request),
            memory=get_memory(request),
            step_id=step_id,
            status="abandoned",
            source="dashboard_ui",
            actor=get_dashboard_actor(request),
            actor_metadata=get_dashboard_actor_metadata(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return render_suggestion_strip(request, format_receipt(receipt))
