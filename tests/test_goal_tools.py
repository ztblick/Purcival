import asyncio
import json

import pytest

from agent import _build_agent_prompt, run_agent_cycle
from dashboard.routes import build_dashboard_model
from goals import SharedGoalStore
from memory import PersonaMemory
from tools import create_tools
from tools.goal_tools import GoalTool, SuggestionTool


def make_store(tmp_path):
    return SharedGoalStore(tmp_path / "user.db")


def make_memory(tmp_path, monkeypatch):
    data_dir = tmp_path / "memory"
    monkeypatch.setattr("memory.DATA_DIR", data_dir)
    return PersonaMemory("jo")


def test_goal_tool_formats_active_goal_context(tmp_path):
    store = make_store(tmp_path)
    career_id = store.create_goal("career", "Learn more about AI safety")
    paused_id = store.create_goal("home", "Repair the fence", status="paused")
    accepted_id = store.create_step(career_id, "Read one alignment paper", status="accepted")
    suggested_id = store.create_step(
        career_id,
        "Review LucidAI's public materials",
        status="suggested",
        source="agent_planning",
    )
    store.create_step(paused_id, "Buy fence boards", status="suggested")

    context = GoalTool(store).get_context()

    assert "GOALS" in context
    assert f"#{career_id} Learn more about AI safety" in context
    assert f"#{accepted_id} [accepted] Read one alignment paper" in context
    assert f"#{suggested_id} [suggested] Review LucidAI's public materials" in context
    assert "Repair the fence" not in context


def test_suggestion_tool_proposes_suggested_agent_step(tmp_path):
    store = make_store(tmp_path)
    goal_id = store.create_goal("health", "Stay active & healthy")
    tool = SuggestionTool(store, created_by_persona="jo")

    result = tool.execute(
        "propose_suggestion",
        goal_id=goal_id,
        title="Pick one short workout for tomorrow",
        rationale="A small concrete choice is easier to accept.",
    )

    steps = store.list_steps(goal_id=goal_id, status="suggested")
    assert "Created suggested step" in result
    assert len(steps) == 1
    assert steps[0]["title"] == "Pick one short workout for tomorrow"
    assert steps[0]["source"] == "agent_planning"
    assert steps[0]["created_by_persona"] == "jo"


def test_suggestion_tool_rejects_inactive_goal_and_open_duplicate(tmp_path):
    store = make_store(tmp_path)
    paused_goal_id = store.create_goal("money", "Make extra money", status="paused")
    active_goal_id = store.create_goal("career", "Learn more about AI safety")
    store.create_step(active_goal_id, "Read one alignment paper", status="suggested")
    tool = SuggestionTool(store)

    with pytest.raises(ValueError, match="not active"):
        tool.execute(
            "propose_suggestion",
            goal_id=paused_goal_id,
            title="List possible weekend gigs",
        )

    with pytest.raises(ValueError, match="already exists"):
        tool.execute(
            "propose_suggestion",
            goal_id=active_goal_id,
            title="Read one alignment paper",
        )


def test_suggestion_tool_status_update_can_store_note(tmp_path):
    store = make_store(tmp_path)
    goal_id = store.create_goal("career", "Learn more about AI safety")
    step_id = store.create_step(goal_id, "Draft three research questions")
    tool = SuggestionTool(store)

    result = tool.execute(
        "update_status",
        step_id=step_id,
        status="rejected",
        note="Too vague for this week.",
    )

    assert result == f"Updated step #{step_id} to rejected"
    assert store.get_step(step_id)["status"] == "rejected"
    feedback = store.list_step_feedback(step_id)
    assert feedback[0]["kind"] == "freeform_note"
    assert feedback[0]["value"] == "Too vague for this week."


def test_create_tools_registers_goal_tools_with_shared_store(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    memory = make_memory(tmp_path, monkeypatch)

    tools = create_tools(memory, goal_store=store)

    assert isinstance(tools["goals"], GoalTool)
    assert isinstance(tools["suggestions"], SuggestionTool)
    assert tools["goals"].store is store
    assert tools["suggestions"].store is store


def test_agent_prompt_distinguishes_planning_from_targeted_cycles():
    base_kwargs = {
        "persona_prompt": "You are Jo.",
        "narrative_state": None,
        "trigger_purpose": "Planning cycle",
        "trigger_time": None,
        "tool_contexts": {"goals": "GOALS\nCareer\n  #1 Learn more about AI safety"},
        "scheduled_plan": None,
        "pending_proposals": [],
        "available_actions": "Tools:\n  - suggestions.propose_suggestion",
        "schedule_config": None,
        "actions_today": 0,
    }

    planning_prompt = _build_agent_prompt(**base_kwargs, is_planning=True)
    targeted_prompt = _build_agent_prompt(**base_kwargs, is_planning=False)

    assert "propose 1-3 concrete one-shot suggestions" in planning_prompt
    assert "Use suggestions.propose_suggestion" in planning_prompt
    assert "This is not a planning cycle" in targeted_prompt
    assert "Do not propose new goal suggestions" in targeted_prompt


def test_planning_cycle_can_create_dashboard_visible_suggestion(
    tmp_path,
    monkeypatch,
):
    store = make_store(tmp_path)
    goal_id = store.create_goal("home", "Be a good husband and father")
    memory = make_memory(tmp_path, monkeypatch)
    tools = create_tools(memory, goal_store=store)

    response = json.dumps([
        {
            "tool": "suggestions",
            "method": "propose_suggestion",
            "parameters": {
                "goal_id": goal_id,
                "title": "Ask what would make tomorrow easier",
                "rationale": "A small check-in can create practical support.",
            },
        }
    ])

    monkeypatch.setattr(
        "agent.brain.ask",
        lambda *args, **kwargs: (
            "<reasoning>One concrete family-support step is useful.</reasoning>\n"
            f"<actions>{response}</actions>\n"
            "<narrative_state>Suggested one family support step.</narrative_state>"
        ),
    )

    trigger = {
        "id": 42,
        "type": "agent_cycle",
        "fire_at": "2026-05-17 08:00:00",
        "context": json.dumps({"purpose": "Planning cycle", "tools": []}),
    }

    assert asyncio.run(run_agent_cycle(trigger, memory, tools, "You are Jo.")) is True

    suggestions = store.list_steps(goal_id=goal_id, status="suggested")
    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "Ask what would make tomorrow easier"
    assert suggestions[0]["source"] == "agent_planning"

    dashboard_model = build_dashboard_model(store)
    assert dashboard_model["suggestions"][0]["display_text"] == "Ask what would make tomorrow easier"
