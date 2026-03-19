"""
Tests for the /schedule trigger preservation fix.

Verifies that:
    1. Targeted wake-ups survive when planning cycles are cleared
    2. ALL planning cycles are removed (regardless of time)
    3. Changing only the action limit doesn't touch any triggers
    4. Legacy and non-agent triggers are never touched
    5. The full _handle_schedule flow preserves targeted wake-ups

Run with: python test_schedule_fix.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory


# --- Helpers ---

def _make_memory():
    """Create a PersonaMemory with a temp directory."""
    tmpdir = tempfile.mkdtemp()
    with patch("memory.DATA_DIR", Path(tmpdir)):
        return PersonaMemory("test_persona")


def _add_planning_cycle(memory, fire_at_str):
    """Add a planning cycle trigger (empty tools list)."""
    return memory.add_trigger(
        trigger_type="agent_cycle",
        fire_at=fire_at_str,
        context=json.dumps({
            "purpose": "Planning cycle — check all tools",
            "tools": [],
        }),
    )


def _add_targeted_wakeup(memory, fire_at_str, purpose="Remind Zach"):
    """Add a targeted wake-up trigger (specific tools)."""
    return memory.add_trigger(
        trigger_type="agent_cycle",
        fire_at=fire_at_str,
        context=json.dumps({
            "purpose": purpose,
            "tools": ["telegram"],
        }),
    )


# --- reschedule_planning_cycles tests ---

def test_targeted_wakeups_preserved():
    """Targeted wake-ups must survive when planning cycles are cleared."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))
    fire_at = tomorrow.replace(hour=15, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S")
    tid = _add_targeted_wakeup(mem, fire_at, "After school check-in")

    removed = mem.reschedule_planning_cycles()

    trigger = mem.get_trigger(tid)
    assert trigger is not None, "Targeted wake-up was deleted!"
    assert removed == 0
    print("  \u2713 targeted_wakeups_preserved")


def test_all_planning_cycles_removed():
    """All planning cycles should be removed regardless of their time."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))

    early = _add_planning_cycle(
        mem, tomorrow.replace(hour=5, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    noon = _add_planning_cycle(
        mem, tomorrow.replace(hour=12, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    evening = _add_planning_cycle(
        mem, tomorrow.replace(hour=20, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))

    removed = mem.reschedule_planning_cycles()

    assert mem.get_trigger(early) is None, "5 AM planning should be removed"
    assert mem.get_trigger(noon) is None, "Noon planning should be removed"
    assert mem.get_trigger(evening) is None, "8 PM planning should be removed"
    assert removed == 3
    print("  \u2713 all_planning_cycles_removed")


def test_mixed_triggers():
    """With a mix, all planning cycles are removed but targeted wake-ups stay."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))

    morning_planning = _add_planning_cycle(
        mem, tomorrow.replace(hour=6, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    midday_planning = _add_planning_cycle(
        mem, tomorrow.replace(hour=13, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    afternoon_target = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=15, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "After school")
    evening_target = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=22, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "Bedtime")

    removed = mem.reschedule_planning_cycles()

    assert mem.get_trigger(morning_planning) is None, "Morning planning should go"
    assert mem.get_trigger(midday_planning) is None, "Midday planning should go"
    assert mem.get_trigger(afternoon_target) is not None, "3:30 PM target stays"
    assert mem.get_trigger(evening_target) is not None, "10 PM target stays"
    assert removed == 2

    active = mem.get_active_triggers()
    assert len(active) == 2, f"Expected 2 remaining, got {len(active)}"
    print("  \u2713 mixed_triggers")


def test_no_triggers():
    """No triggers shouldn't error."""
    mem = _make_memory()
    removed = mem.reschedule_planning_cycles()
    assert removed == 0
    print("  \u2713 no_triggers")


def test_legacy_triggers_untouched():
    """Triggers with plain text context should be left alone."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))
    fire_at = tomorrow.replace(hour=3, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
    tid = mem.add_trigger("agent_cycle", fire_at, context="Plain text")

    removed = mem.reschedule_planning_cycles()

    assert mem.get_trigger(tid) is not None, "Legacy trigger should stay"
    assert removed == 0
    print("  \u2713 legacy_triggers_untouched")


def test_non_agent_triggers_untouched():
    """Reminder and other trigger types should never be touched."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))
    fire_at = tomorrow.replace(hour=3, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
    tid = mem.add_trigger("reminder", fire_at, context="Pick up groceries")

    removed = mem.reschedule_planning_cycles()

    assert mem.get_trigger(tid) is not None, "Reminder trigger should stay"
    assert removed == 0
    print("  \u2713 non_agent_triggers_untouched")


# --- Integration tests: simulate _handle_schedule behavior ---

def test_schedule_change_preserves_targeted_wakeups():
    """
    Simulate the full /schedule flow: agent has a day planned with
    targeted wake-ups, user changes operating hours, all planning
    cycles are removed but targeted wake-ups survive.
    """
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))

    # Set initial schedule
    mem.set_schedule_config("09:00", "23:00", 30, 25)

    # Agent has planned its day: planning cycles + targeted wake-ups
    _add_planning_cycle(
        mem, tomorrow.replace(hour=9, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    _add_planning_cycle(
        mem, tomorrow.replace(hour=13, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    target_school = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=15, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "After school check-in")
    target_volleyball = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=16, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "Pre-volleyball reminder")
    target_bedtime = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=22, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "Bedtime wind-down")

    assert len(mem.get_active_triggers()) == 5

    # User changes wake time from 09:00 to 06:00
    new_start, new_end = "06:00", "23:00"
    current = mem.get_schedule_config()
    hours_changed = (
        current["start_time"] != new_start
        or current["end_time"] != new_end
    )
    assert hours_changed

    mem.set_schedule_config(new_start, new_end, 30, 25)
    removed = mem.reschedule_planning_cycles()

    # Both planning cycles removed (9 AM and 1 PM)
    # All three targeted wake-ups stay
    assert removed == 2, f"Expected 2 removals, got {removed}"

    active = mem.get_active_triggers()
    assert len(active) == 3, f"Expected 3 remaining, got {len(active)}"

    assert mem.get_trigger(target_school) is not None, "School target deleted!"
    assert mem.get_trigger(target_volleyball) is not None, "Volleyball target deleted!"
    assert mem.get_trigger(target_bedtime) is not None, "Bedtime target deleted!"

    print("  \u2713 schedule_change_preserves_targeted_wakeups")


def test_action_limit_only_change():
    """Changing only the action limit should not touch any triggers."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))

    mem.set_schedule_config("06:00", "23:00", 30, 25)

    # Agent's plan
    planning = _add_planning_cycle(
        mem, tomorrow.replace(hour=6, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    target = _add_targeted_wakeup(
        mem, tomorrow.replace(hour=15, minute=30, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "After school")

    assert len(mem.get_active_triggers()) == 2

    # "Change" schedule with same hours but different action limit
    current = mem.get_schedule_config()
    hours_changed = (
        current["start_time"] != "06:00"
        or current["end_time"] != "23:00"
    )
    assert not hours_changed, "Hours should be the same"

    # Just update config, don't touch triggers
    mem.set_schedule_config("06:00", "23:00", 30, 50)

    # BOTH triggers should still be there (planning + targeted)
    assert len(mem.get_active_triggers()) == 2
    assert mem.get_trigger(planning) is not None, "Planning cycle deleted!"
    assert mem.get_trigger(target) is not None, "Targeted wake-up deleted!"

    print("  \u2713 action_limit_only_change")


# --- Planning Cycle Guarantee Tests ---

def test_has_future_planning_cycle_true():
    """Should detect a future planning cycle."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))
    _add_planning_cycle(
        mem, tomorrow.replace(hour=6, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))
    assert mem.has_future_planning_cycle()
    print("  \u2713 has_future_planning_cycle_true")


def test_has_future_planning_cycle_false_with_only_targeted():
    """Targeted wake-ups should NOT satisfy the planning cycle check."""
    mem = _make_memory()
    tomorrow = (datetime.now() + timedelta(days=1))
    _add_targeted_wakeup(
        mem, tomorrow.replace(hour=6, minute=50, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "Morning message")
    assert not mem.has_future_planning_cycle()
    print("  \u2713 has_future_planning_cycle_false_with_only_targeted")


def test_has_future_planning_cycle_false_when_empty():
    """No triggers at all should return False."""
    mem = _make_memory()
    assert not mem.has_future_planning_cycle()
    print("  \u2713 has_future_planning_cycle_false_when_empty")


def test_bootstrap_seeds_planning_despite_targeted_triggers():
    """
    ensure_agent_has_plan should seed a planning cycle even when
    targeted wake-ups exist. This is the core bug fix — the old
    code saw any future trigger and assumed the agent was fine.
    """
    from proactive import ensure_agent_has_plan

    mem = _make_memory()
    mem.set_schedule_config("06:00", "23:00", 30, 25)
    tomorrow = (datetime.now() + timedelta(days=1))

    # Agent has a targeted wake-up but no planning cycle
    _add_targeted_wakeup(
        mem, tomorrow.replace(hour=6, minute=50, second=0).strftime("%Y-%m-%d %H:%M:%S"),
        "Morning message to Zach")

    assert len(mem.get_active_triggers()) == 1
    assert not mem.has_future_planning_cycle()

    # Bootstrap should seed a planning cycle
    ensure_agent_has_plan(mem)

    assert len(mem.get_active_triggers()) == 2
    assert mem.has_future_planning_cycle()
    print("  \u2713 bootstrap_seeds_planning_despite_targeted_triggers")


def test_bootstrap_does_not_duplicate_planning_cycle():
    """If a planning cycle already exists, bootstrap should not add another."""
    from proactive import ensure_agent_has_plan

    mem = _make_memory()
    mem.set_schedule_config("06:00", "23:00", 30, 25)
    tomorrow = (datetime.now() + timedelta(days=1))

    _add_planning_cycle(
        mem, tomorrow.replace(hour=6, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S"))

    ensure_agent_has_plan(mem)

    # Should still be just 1 planning cycle, not 2
    active = mem.get_active_triggers()
    planning_cycles = []
    for t in active:
        try:
            ctx = json.loads(t["context"]) if t["context"] else {}
            if len(ctx.get("tools", [])) == 0:
                planning_cycles.append(t)
        except (json.JSONDecodeError, TypeError):
            pass

    assert len(planning_cycles) == 1, f"Expected 1 planning cycle, got {len(planning_cycles)}"
    print("  \u2713 bootstrap_does_not_duplicate_planning_cycle")


# --- Run all tests ---

if __name__ == "__main__":
    print("\nRunning /schedule trigger preservation tests...\n")

    tests = [
        test_targeted_wakeups_preserved,
        test_all_planning_cycles_removed,
        test_mixed_triggers,
        test_no_triggers,
        test_legacy_triggers_untouched,
        test_non_agent_triggers_untouched,
        test_schedule_change_preserves_targeted_wakeups,
        test_action_limit_only_change,
        test_has_future_planning_cycle_true,
        test_has_future_planning_cycle_false_with_only_targeted,
        test_has_future_planning_cycle_false_when_empty,
        test_bootstrap_seeds_planning_despite_targeted_triggers,
        test_bootstrap_does_not_duplicate_planning_cycle,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  \u2717 {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*50}\n")
    sys.exit(0 if failed == 0 else 1)
