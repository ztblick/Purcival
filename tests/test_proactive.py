"""
Tests for the proactive messaging module.

Run with: python test_proactive.py

Tests verify:
    - Trigger storage and retrieval in the database
    - Agent planning-cycle bootstrap is idempotent
    - Targeted wake-ups do not count as planning cycles
    - Trigger marking and advancement
"""

import json
import sys
import shutil
from pathlib import Path
from datetime import datetime, timedelta

import memory
import proactive

# Use test directory
TEST_DATA_DIR = Path(__file__).parent / "test_data_proactive"
memory.DATA_DIR = TEST_DATA_DIR


def cleanup():
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)


def _reset_data_dir():
    memory.DATA_DIR = TEST_DATA_DIR


def setup_module():
    cleanup()
    _reset_data_dir()


def teardown_module():
    cleanup()


def test_add_and_retrieve_trigger():
    """Triggers should be stored and retrievable."""
    print("  test_add_and_retrieve_trigger...", end=" ")

    mem = memory.PersonaMemory("test_triggers")

    # Add a trigger that's already past due
    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    tid = mem.add_trigger("reminder", past, context="Pick up Tessa")

    assert tid == 1

    due = mem.get_due_triggers()
    assert len(due) == 1
    assert due[0]["type"] == "reminder"
    assert due[0]["context"] == "Pick up Tessa"

    print("PASS")


def test_mark_trigger_fired():
    """Fired triggers should not appear in due triggers."""
    print("  test_mark_trigger_fired...", end=" ")

    mem = memory.PersonaMemory("test_mark_fired")
    past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    tid = mem.add_trigger("reminder", past, context="Pick up Tessa")

    assert len(mem.get_due_triggers()) == 1

    mem.mark_trigger_fired(tid)

    due_after = mem.get_due_triggers()
    assert len(due_after) == 0

    print("PASS")


def test_future_trigger_not_due():
    """Triggers in the future should not appear as due."""
    print("  test_future_trigger_not_due...", end=" ")

    mem = memory.PersonaMemory("test_future")

    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    mem.add_trigger("check_in", future, context="Future check-in")

    due = mem.get_due_triggers()
    assert len(due) == 0

    active = mem.get_active_triggers()
    assert len(active) == 1

    print("PASS")


def test_ensure_agent_has_plan_no_schedule_noops():
    """Without a schedule config, the agent should stay passive."""
    print("  test_ensure_agent_has_plan_no_schedule_noops...", end=" ")

    mem = memory.PersonaMemory("test_no_schedule")

    proactive.ensure_agent_has_plan(mem)

    assert mem.get_active_triggers() == []
    print("PASS")


def test_ensure_agent_has_plan_seeds_planning_cycle():
    """A configured persona should get one future planning cycle."""
    print("  test_ensure_agent_has_plan_seeds_planning_cycle...", end=" ")

    mem = memory.PersonaMemory("test_seed_plan")
    mem.set_schedule_config("06:00", "23:00", 30, 25)

    proactive.ensure_agent_has_plan(mem)

    active = mem.get_active_triggers()
    assert len(active) == 1
    assert active[0]["type"] == "agent_cycle"
    context = json.loads(active[0]["context"])
    assert context["tools"] == []
    assert "Planning cycle" in context["purpose"]
    print("PASS")


def test_ensure_agent_has_plan_idempotent():
    """Calling bootstrap twice should not duplicate planning cycles."""
    print("  test_ensure_agent_has_plan_idempotent...", end=" ")

    mem = memory.PersonaMemory("test_seed_idempotent")
    mem.set_schedule_config("06:00", "23:00", 30, 25)

    proactive.ensure_agent_has_plan(mem)
    proactive.ensure_agent_has_plan(mem)

    assert len(mem.get_active_triggers()) == 1
    print("PASS")


def test_targeted_wakeup_does_not_satisfy_planning_cycle():
    """The agent still needs planning if only targeted wake-ups exist."""
    print("  test_targeted_wakeup_does_not_satisfy_planning_cycle...", end=" ")

    mem = memory.PersonaMemory("test_targeted_only")
    mem.set_schedule_config("06:00", "23:00", 30, 25)
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    mem.add_trigger(
        "agent_cycle",
        future,
        context=json.dumps({
            "purpose": "Follow up on a specific event",
            "tools": ["google_calendar"],
        }),
    )

    proactive.ensure_agent_has_plan(mem)

    active = mem.get_active_triggers()
    planning = [
        trigger for trigger in active
        if json.loads(trigger["context"]).get("tools") == []
    ]
    assert len(active) == 2
    assert len(planning) == 1
    print("PASS")


def test_delete_trigger():
    """Deleted triggers should be gone entirely."""
    print("  test_delete_trigger...", end=" ")

    mem = memory.PersonaMemory("test_delete")

    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    tid = mem.add_trigger("reminder", past, context="Test")

    mem.delete_trigger(tid)

    assert len(mem.get_due_triggers()) == 0
    assert len(mem.get_active_triggers()) == 0

    print("PASS")


if __name__ == "__main__":
    cleanup()

    print("\nRunning proactive messaging tests...\n")

    tests = [
        test_add_and_retrieve_trigger,
        test_mark_trigger_fired,
        test_future_trigger_not_due,
        test_ensure_agent_has_plan_no_schedule_noops,
        test_ensure_agent_has_plan_seeds_planning_cycle,
        test_ensure_agent_has_plan_idempotent,
        test_targeted_wakeup_does_not_satisfy_planning_cycle,
        test_delete_trigger,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"FAIL — {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    cleanup()

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}\n")

    sys.exit(0 if failed == 0 else 1)
