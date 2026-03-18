"""
Tests for the proactive messaging module.

Run with: python test_proactive.py

Tests verify:
    - Trigger storage and retrieval in the database
    - Hourly trigger seeding is idempotent
    - Decision gate respects active conversation window
    - Decision gate always fires for reminders
    - Trigger marking and advancement
"""

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

    mem = memory.PersonaMemory("test_triggers")

    due_before = mem.get_due_triggers()
    assert len(due_before) == 1

    mem.mark_trigger_fired(1)

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


def test_seed_hourly_idempotent():
    """Seeding hourly triggers twice should not create duplicates."""
    print("  test_seed_hourly_idempotent...", end=" ")

    mem = memory.PersonaMemory("test_seed")

    proactive.seed_hourly_triggers(mem)
    count1 = len(mem.get_active_triggers())

    proactive.seed_hourly_triggers(mem)
    count2 = len(mem.get_active_triggers())

    assert count1 == count2, f"Expected {count1} triggers, got {count2} after re-seed"
    assert count1 > 0, "Should have created at least some triggers"

    print(f"PASS ({count1} triggers)")


def test_decision_gate_reminder_always_sends():
    """Reminders should always fire regardless of conversation state."""
    print("  test_decision_gate_reminder_always_sends...", end=" ")

    mem = memory.PersonaMemory("test_gate_reminder")

    # Add a message that was sent just now (active conversation)
    mem.add_message("user", "I'm busy right now")

    trigger = {
        "id": 1,
        "type": "reminder",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "context": "Pick up Tessa",
        "recurring": None,
    }

    assert proactive._should_send(trigger, mem) is True

    print("PASS")


def test_decision_gate_checkin_skips_active():
    """Check-ins should be skipped if conversation is active."""
    print("  test_decision_gate_checkin_skips_active...", end=" ")

    mem = memory.PersonaMemory("test_gate_checkin")

    # Add a message from just now
    mem.add_message("user", "Just sent this")

    trigger = {
        "id": 1,
        "type": "check_in",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "context": "Hourly check-in",
        "recurring": "hourly_check_in",
    }

    assert proactive._should_send(trigger, mem) is False

    print("PASS")


def test_decision_gate_checkin_sends_when_idle():
    """Check-ins should fire if no recent conversation."""
    print("  test_decision_gate_checkin_sends_when_idle...", end=" ")

    mem = memory.PersonaMemory("test_gate_idle")

    # Add a message from an hour ago
    old_time = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = mem._connect()
    conn.execute(
        "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
        ("user", "Old message", old_time),
    )
    conn.commit()
    conn.close()

    trigger = {
        "id": 1,
        "type": "check_in",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "context": "Hourly check-in",
        "recurring": "hourly_check_in",
    }

    assert proactive._should_send(trigger, mem) is True

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
        test_seed_hourly_idempotent,
        test_decision_gate_reminder_always_sends,
        test_decision_gate_checkin_skips_active,
        test_decision_gate_checkin_sends_when_idle,
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
