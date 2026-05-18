import asyncio
import json

import pytest

from agent import _build_agent_prompt, run_agent_cycle
from dashboard.routes import build_dashboard_model
from goals import SharedGoalStore
from memory import PersonaMemory
from tools import create_tools
from tools.goal_tools import GoalTool, SuggestionTool
from tools.opportunity_tool import OpportunityTool


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
    assert isinstance(tools["opportunities"], OpportunityTool)
    assert isinstance(tools["suggestions"], SuggestionTool)
    assert tools["goals"].store is store
    assert tools["opportunities"].store is store
    assert tools["suggestions"].store is store
    assert tools["suggestions"].memory is memory


def test_agent_prompt_distinguishes_planning_from_targeted_cycles():
    base_kwargs = {
        "persona_prompt": "You are Jo.",
        "narrative_state": None,
        "trigger_purpose": "Planning cycle",
        "trigger_time": None,
        "tool_contexts": {"goals": "GOALS\nCareer\n  #1 Learn more about AI safety"},
        "scheduled_plan": None,
        "pending_proposals": [],
        "available_actions": "Tools:\n  - opportunities.propose_goal_step",
        "schedule_config": None,
        "actions_today": 0,
    }

    planning_prompt = _build_agent_prompt(**base_kwargs, is_planning=True)
    targeted_prompt = _build_agent_prompt(**base_kwargs, is_planning=False)

    assert "propose 1-3 concrete one-shot suggestions" in planning_prompt
    assert "Use opportunities.propose_goal_step" in planning_prompt
    assert "Do not call suggestions.propose_suggestion directly" in planning_prompt
    assert "This is not a planning cycle" in targeted_prompt
    assert "Do not propose new goal suggestions" in targeted_prompt


def test_opportunity_tool_records_and_delivers_goal_step(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    goal_id = store.create_goal("career", "Learn more about AI safety")
    memory = make_memory(tmp_path, monkeypatch)
    tool = OpportunityTool(memory, store, created_by_persona="jo")

    result = tool.execute(
        "propose_goal_step",
        goal_id=goal_id,
        title="Draft three alignment questions",
        rationale="A question list makes the next reading session sharper.",
    )

    opportunities = memory.list_agent_opportunities(kind="suggest_goal_step")
    steps = store.list_steps(goal_id=goal_id, status="suggested")
    events = memory.get_agent_events(event_type="opportunity_delivered")

    assert "Recorded opportunity" in result
    assert len(opportunities) == 1
    assert opportunities[0]["status"] == "delivered"
    assert opportunities[0]["step_id"] == steps[0]["id"]
    assert "delivered_step_id" in opportunities[0]["proposed_action_json"]
    assert steps[0]["title"] == "Draft three alignment questions"
    assert steps[0]["source"] == "agent_planning"
    assert len(events) == 1


def test_suggestion_tool_status_update_writes_receipt_event(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    goal_id = store.create_goal("career", "Learn more about AI safety")
    step_id = store.create_step(goal_id, "Read one alignment paper", status="accepted")
    memory = make_memory(tmp_path, monkeypatch)
    tool = SuggestionTool(store, created_by_persona="jo", memory=memory)

    result = tool.execute(
        "complete_step",
        step_id=step_id,
        note="Zach said he finished it.",
    )

    events = memory.get_agent_events(event_type="step_completed")
    feedback = store.list_step_feedback(step_id)

    assert result == f"Updated step #{step_id} to completed"
    assert store.get_step(step_id)["status"] == "completed"
    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["previous_status"] == "accepted"
    assert feedback[0]["kind"] == "completion_note"


def test_accepting_step_creates_accountability_opportunity(tmp_path, monkeypatch):
    from accountability import record_step_status_change

    store = make_store(tmp_path)
    goal_id = store.create_goal("health", "Stay active & healthy")
    step_id = store.create_step(goal_id, "Take a twenty minute walk")
    memory = make_memory(tmp_path, monkeypatch)

    receipt = record_step_status_change(
        store=store,
        memory=memory,
        step_id=step_id,
        status="accepted",
        source="test",
    )

    opportunities = memory.list_agent_opportunities(kind="accountability_check")
    events = memory.get_agent_events(event_type="step_accepted")

    assert receipt["accountability_opportunity_id"] == opportunities[0]["id"]
    assert store.get_step(step_id)["status"] == "accepted"
    assert opportunities[0]["step_id"] == step_id
    assert opportunities[0]["status"] in {"scheduled", "queued"}
    assert len(events) == 1


def test_opportunity_tool_suppresses_dismissed_repeat(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    goal_id = store.create_goal("health", "Stay active & healthy")
    memory = make_memory(tmp_path, monkeypatch)
    tool = OpportunityTool(memory, store, created_by_persona="jo")

    first = tool.execute(
        "propose_goal_step",
        goal_id=goal_id,
        title="Pick a short walk window",
        rationale="A specific window makes the step easier to accept.",
    )
    opportunity = memory.list_agent_opportunities(kind="suggest_goal_step")[0]
    tool.execute(
        "dismiss_opportunity",
        opportunity_id=opportunity["id"],
        reason="Not useful this week.",
    )
    second = tool.execute(
        "propose_goal_step",
        goal_id=goal_id,
        title="Pick a short walk window",
        rationale="Try the same idea again.",
    )

    assert "Recorded opportunity" in first
    assert "Suppressed similar opportunity" in second
    assert len(memory.list_agent_opportunities(kind="suggest_goal_step")) == 1
    assert len(store.list_steps(goal_id=goal_id, status="suggested")) == 1


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
            "tool": "opportunities",
            "method": "propose_goal_step",
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
    opportunities = memory.list_agent_opportunities(kind="suggest_goal_step")
    assert len(suggestions) == 1
    assert suggestions[0]["title"] == "Ask what would make tomorrow easier"
    assert suggestions[0]["source"] == "agent_planning"
    assert len(opportunities) == 1
    assert opportunities[0]["status"] == "delivered"
    assert opportunities[0]["step_id"] == suggestions[0]["id"]
    assert json.loads(opportunities[0]["evidence_event_ids"])

    dashboard_model = build_dashboard_model(store)
    assert dashboard_model["suggestions"][0]["display_text"] == "Ask what would make tomorrow easier"


def test_planning_cycle_routes_legacy_direct_suggestion_through_opportunity(
    tmp_path,
    monkeypatch,
):
    store = make_store(tmp_path)
    goal_id = store.create_goal("money", "Make some extra money")
    memory = make_memory(tmp_path, monkeypatch)
    tools = create_tools(memory, goal_store=store)

    response = json.dumps([
        {
            "tool": "suggestions",
            "method": "propose_suggestion",
            "parameters": {
                "goal_id": goal_id,
                "title": "List two low-effort tutoring offers",
                "rationale": "This keeps the money goal concrete.",
            },
        }
    ])

    monkeypatch.setattr(
        "agent.brain.ask",
        lambda *args, **kwargs: (
            "<reasoning>Use the old direct suggestion action.</reasoning>\n"
            f"<actions>{response}</actions>\n"
            "<narrative_state>Suggested one money step.</narrative_state>"
        ),
    )

    trigger = {
        "id": 43,
        "type": "agent_cycle",
        "fire_at": "2026-05-17 08:00:00",
        "context": json.dumps({
            "purpose": "Planning cycle",
            "job_type": "planning",
            "tools": [],
        }),
    }

    assert asyncio.run(run_agent_cycle(trigger, memory, tools, "You are Jo.")) is True

    suggestions = store.list_steps(goal_id=goal_id, status="suggested")
    opportunities = memory.list_agent_opportunities(kind="suggest_goal_step")
    conn = memory._connect()
    actions = conn.execute(
        "SELECT tool_name, method_name FROM agent_actions ORDER BY id ASC"
    ).fetchall()
    conn.close()

    assert len(suggestions) == 1
    assert len(opportunities) == 1
    assert opportunities[0]["step_id"] == suggestions[0]["id"]
    assert actions[0]["tool_name"] == "opportunities"
