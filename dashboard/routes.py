"""Dashboard routes for seed-backed Goals dashboard rendering."""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from dashboard.motivation import title_for_date
from goals import SharedGoalStore


TEMPLATE_DIR = Path(__file__).parent / "templates"

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATE_DIR)


def category_class(category: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "-"
        for character in category.strip().lower()
    )
    return f"category-{cleaned.strip('-') or 'general'}"


def get_store() -> SharedGoalStore:
    db_path = os.environ.get("PURCIVAL_GOALS_DB")
    return SharedGoalStore(Path(db_path)) if db_path else SharedGoalStore()


def build_dashboard_model(store: SharedGoalStore) -> dict[str, Any]:
    goals = store.list_goals(status="active")
    steps = store.list_steps()
    goals_by_id = {goal["id"]: goal for goal in goals}

    goal_cards = []
    for goal in goals:
        goal_cards.append({
            **goal,
            "category_class": category_class(goal["category"]),
        })

    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for goal in goal_cards:
        categories[goal["category"]].append(goal)

    suggestions = build_step_cards(steps, goals_by_id, "suggested")
    accepted_steps = build_step_cards(steps, goals_by_id, "accepted")

    active_context = suggestions[0] if suggestions else accepted_steps[0] if accepted_steps else None

    return {
        "categories": dict(categories),
        "goals": goal_cards,
        "suggestions": suggestions,
        "accepted_steps": accepted_steps,
        "active_context": active_context,
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
        cards.append({
            **step,
            "goal": goal,
            "category_class": category_class(goal["category"]) if goal else "category-general",
        })
    return cards


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
    model = build_dashboard_model(get_store())
    return templates.TemplateResponse(
        request,
        "partials/suggestion_strip.html",
        model,
    )


@router.get("/partials/chat")
def chat_panel(request: Request):
    model = build_dashboard_model(get_store())
    return templates.TemplateResponse(
        request,
        "partials/chat_panel.html",
        model,
    )
