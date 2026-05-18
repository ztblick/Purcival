import asyncio
import json
from pathlib import Path

import context
import memory as memory_module
from accountability import record_step_status_change
from agent import run_agent_cycle
from delivery import mark_inbox_item
from goals import SharedGoalStore
from memory import PersonaMemory
from reflection import run_reflection_job
from tools import create_tools


def _configure_test_paths(tmp_path: Path, monkeypatch):
    persona_data_dir = tmp_path / "persona_data"
    monkeypatch.setattr(memory_module, "DATA_DIR", persona_data_dir)
    monkeypatch.setattr(context, "DATA_DIR", persona_data_dir)
    monkeypatch.setattr(context, "USER_CONTEXT_PATH", persona_data_dir / "user_context.md")


def test_memory_item_validation_and_status_lifecycle(tmp_path, monkeypatch):
    _configure_test_paths(tmp_path, monkeypatch)
    mem = PersonaMemory("jo")
    evidence_event_id = mem.add_agent_event(
        event_type="step_accepted",
        source="test",
        source_id="1",
        payload={"step_id": 1},
        schedule_reflection=False,
    )

    try:
        mem.record_memory_item(
            kind="fact",
            subject="invalid",
            content="missing evidence",
            confidence=3,
            evidence_event_ids=[],
        )
        assert False, "record_memory_item should require evidence"
    except ValueError:
        pass

    item_id = mem.record_memory_item(
        kind="fact",
        subject="step:1:outcome",
        content="Zach completed a test step.",
        confidence=3,
        evidence_event_ids=[evidence_event_id],
    )
    item = mem.get_memory_item(item_id)
    assert item is not None
    assert item["status"] == "active"

    try:
        mem.record_memory_item(
            kind="preference",
            subject="identity",
            content="Political preference inferred from one message.",
            confidence=4,
            evidence_event_ids=[evidence_event_id],
        )
        assert False, "sensitive high-confidence memories should be rejected"
    except ValueError:
        pass

    assert mem.update_memory_item_status(item_id, "superseded") is True
    updated = mem.get_memory_item(item_id)
    assert updated is not None
    assert updated["status"] == "superseded"

    try:
        mem.update_memory_item_status(item_id, "active")
        assert False, "superseded memories should not become active again"
    except ValueError:
        pass


def test_reflection_job_processes_events_idempotently(tmp_path, monkeypatch):
    _configure_test_paths(tmp_path, monkeypatch)
    mem = PersonaMemory("jo")
    store = SharedGoalStore(tmp_path / "user.db")
    goal_id = store.create_goal("career", "Learn more about AI safety")
    step_id = store.create_step(goal_id, "Read one alignment paper", status="suggested")

    record_step_status_change(
        store,
        mem,
        step_id=step_id,
        status="accepted",
        source="dashboard_ui",
        actor="zach_dashboard",
    )
    record_step_status_change(
        store,
        mem,
        step_id=step_id,
        status="completed",
        source="dashboard_ui",
        actor="zach_dashboard",
    )

    first = run_reflection_job(mem, store=store)
    second = run_reflection_job(mem, store=store)

    assert len(first["processed_event_ids"]) == 2
    assert second["processed_event_ids"] == []

    commitment = mem.list_memory_items(kind="commitment", status="superseded")
    outcomes = mem.list_memory_items(kind="fact", status="active")
    assert commitment
    assert any("completed the step" in item["content"] for item in outcomes)

    events = mem.get_agent_events(event_type="step_completed")
    assert events[0]["processed_at"] is not None


def test_reflection_learns_from_ignored_suggestion_pattern(tmp_path, monkeypatch):
    _configure_test_paths(tmp_path, monkeypatch)
    mem = PersonaMemory("jo")
    store = SharedGoalStore(tmp_path / "user.db")
    goal_id = store.create_goal("money", "Make some extra money")

    def create_dismissed_card(title: str):
        step_id = store.create_step(goal_id, title, status="suggested")
        opportunity_id = mem.add_agent_opportunity(
            kind="suggest_goal_step",
            title=title,
            rationale="Possible proactive idea.",
            goal_id=goal_id,
            step_id=step_id,
            status="delivered",
            duplicate_key=f"suggest_goal_step:{title.casefold()}",
        )
        item_id = mem.add_agent_inbox_item(
            opportunity_id=opportunity_id,
            priority=3,
            surface="dashboard",
            title=f"Suggested step: {title}",
            body="Try this next.",
        )
        mark_inbox_item(mem, item_id, "dismissed", reason="too_noisy")

    create_dismissed_card("Offer one hour of tutoring")
    create_dismissed_card("Draft a side-hustle idea list")

    receipt = run_reflection_job(mem, store=store)

    assert receipt["processed_event_ids"]
    preferences = mem.list_memory_items(kind="preference", status="active")
    assert any(
        item["subject"] == "pattern:ignored_inbox:suggest_goal_step"
        for item in preferences
    )


def test_context_includes_structured_memory(tmp_path, monkeypatch):
    _configure_test_paths(tmp_path, monkeypatch)
    mem = PersonaMemory("jo")
    evidence_event_id = mem.add_agent_event(
        event_type="step_completed",
        source="test",
        source_id="7",
        payload={"step_id": 7},
        schedule_reflection=False,
    )
    mem.record_memory_item(
        kind="fact",
        subject="step:7:outcome",
        content="Zach completed the step 'Read one alignment paper'.",
        confidence=4,
        evidence_event_ids=[evidence_event_id],
    )
    mem.add_message("user", "What should I do next for AI safety?")

    system_prompt, messages = context.assemble_context("You are Jo.", mem)

    assert "STRUCTURED MEMORY" in system_prompt
    assert "Read one alignment paper" in system_prompt
    assert messages


def test_agent_cycle_handles_reflection_job_type(tmp_path, monkeypatch):
    _configure_test_paths(tmp_path, monkeypatch)
    mem = PersonaMemory("jo")
    mem.add_message("user", "No, don't suggest that again.")

    trigger_id = mem.add_trigger(
        "agent_cycle",
        "2026-12-25 10:00:00",
        context=json.dumps(
            {
                "job_type": "reflection",
                "purpose": "Process recent feedback",
                "tools": [],
            }
        ),
    )
    trigger = mem.get_trigger(trigger_id)
    store = SharedGoalStore(tmp_path / "user.db")
    tools = create_tools(mem, goal_store=store)

    result = asyncio.run(
        run_agent_cycle(
            trigger=trigger,
            memory=mem,
            tools=tools,
            persona_prompt="You are Jo.",
        )
    )

    assert result is True
    job = mem.get_agent_job_for_trigger(trigger_id)
    assert job is not None
    assert job["status"] == "completed"
    preferences = mem.list_memory_items(kind="preference", status="active")
    assert any(item["subject"].startswith("correction:message:") for item in preferences)
