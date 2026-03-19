"""
Tests for tool context caching and conversation integration.

Verifies that:
    1. Tool contexts are cached during agent cycles
    2. Cached contexts appear in conversation prompts
    3. Past events are filtered from cached context
    4. Freshness timestamps are included
    5. Empty/missing caches produce no output
    6. Schedule tool context is NOT cached (already shown separately)
    7. Midnight-wrapping events are handled correctly

Run with: python tests/test_tool_context_cache.py

Note: Tests that go through _load_tool_contexts patch datetime.now()
in context.py to a fixed 10:00 AM so results are deterministic
regardless of when tests actually run.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory
from context import (
    _load_tool_contexts,
    _filter_past_events,
    _extract_end_time,
    _get_cached_tool_names,
)

# Fixed reference time for deterministic tests: 10:00 AM today
_TODAY = datetime.now().date()
_NOW = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 10, 0, 0)


# --- Helpers ---

def _make_memory():
    """Create a PersonaMemory with a temp directory."""
    tmpdir = tempfile.mkdtemp()
    with patch("memory.DATA_DIR", Path(tmpdir)):
        return PersonaMemory("test_persona")


def _cache_context(memory, tool_name, context, minutes_ago=0):
    """
    Simulate caching a tool context as the agent cycle would.

    Uses the fixed _NOW reference for the cached_at timestamp so
    freshness calculations are deterministic.
    """
    cached_at = (
        _NOW - timedelta(minutes=minutes_ago)
    ).strftime("%Y-%m-%d %H:%M:%S")
    memory.set_tool_state(tool_name, "cached_context", context)
    memory.set_tool_state(tool_name, "cached_context_at", cached_at)


def _load_tool_contexts_at_fixed_time(memory):
    """
    Call _load_tool_contexts with datetime.now() patched to _NOW (10 AM).

    This ensures the freshness calculation and past-event filtering
    use our fixed reference time, not the real wall clock.
    """
    with patch("context.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        mock_dt.strptime = datetime.strptime
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        return _load_tool_contexts(memory)


# --- Cache Discovery ---

def test_no_cached_tools():
    """No cached data should produce empty string."""
    mem = _make_memory()
    result = _load_tool_contexts_at_fixed_time(mem)
    assert result == ""
    print("  \u2713 no_cached_tools")


def test_discover_cached_tools():
    """Should find tools that have cached context."""
    mem = _make_memory()
    _cache_context(mem, "google_calendar",
                   "UPCOMING:\n  3:00 PM \u2013 4:00 PM  \"Meeting\" [Work]")
    names = _get_cached_tool_names(mem)
    assert "google_calendar" in names
    print("  \u2713 discover_cached_tools")


# --- Context Loading ---
# All times are relative to _NOW (10:00 AM).
# 2:00 PM is 4 hours in the future. 7:00 AM is 3 hours in the past.

def test_cached_context_appears_in_prompt():
    """Cached tool context should appear in the loaded output."""
    mem = _make_memory()
    context = "UPCOMING:\n  2:00 PM \u2013 3:00 PM  \"Team Meeting\" (Room 204) [School]"
    _cache_context(mem, "google_calendar", context, minutes_ago=5)

    result = _load_tool_contexts_at_fixed_time(mem)
    assert "GOOGLE CALENDAR" in result
    assert "Team Meeting" in result
    assert "updated" in result
    print("  \u2713 cached_context_appears_in_prompt")


def test_freshness_just_updated():
    """Context cached < 2 minutes ago should say 'just updated'."""
    mem = _make_memory()
    _cache_context(mem, "google_calendar",
                   "UPCOMING:\n  2:00 PM \u2013 3:00 PM  \"Meeting\" [Personal]",
                   minutes_ago=1)

    result = _load_tool_contexts_at_fixed_time(mem)
    assert "just updated" in result
    print("  \u2713 freshness_just_updated")


def test_freshness_minutes_ago():
    """Context cached 30 minutes ago should show minutes."""
    mem = _make_memory()
    _cache_context(mem, "google_calendar",
                   "UPCOMING:\n  2:00 PM \u2013 3:00 PM  \"Meeting\" [Personal]",
                   minutes_ago=30)

    result = _load_tool_contexts_at_fixed_time(mem)
    assert "30 minutes ago" in result
    print("  \u2713 freshness_minutes_ago")


def test_freshness_hours_ago():
    """Context cached 3 hours ago should show hours."""
    mem = _make_memory()
    _cache_context(mem, "google_calendar",
                   "UPCOMING:\n  5:00 PM \u2013 6:00 PM  \"Meeting\" [Personal]",
                   minutes_ago=180)

    result = _load_tool_contexts_at_fixed_time(mem)
    assert "3 hours ago" in result
    print("  \u2713 freshness_hours_ago")


# --- Past Event Filtering ---
# These tests use _filter_past_events directly with fixed _NOW.

def test_filter_removes_past_events():
    """Events that have ended should be filtered out."""
    context = (
        "UPCOMING:\n"
        "  8:00 AM \u2013 9:00 AM  \"Past Meeting\" [School]\n"
        "  2:00 PM \u2013 3:00 PM  \"Future Meeting\" [Personal]"
    )
    filtered = _filter_past_events(context, _NOW)
    assert "Past Meeting" not in filtered
    assert "Future Meeting" in filtered
    print("  \u2713 filter_removes_past_events")


def test_filter_keeps_future_events():
    """Events still upcoming should be preserved."""
    context = "UPCOMING:\n  11:00 AM \u2013 12:00 PM  \"Team Standup\" [School]"
    filtered = _filter_past_events(context, _NOW)
    assert "Team Standup" in filtered
    print("  \u2713 filter_keeps_future_events")


def test_filter_keeps_allday_events():
    """All-day events should always be kept."""
    context = "ALL DAY:\n  Mom's Birthday [Personal]\n  St. Patrick's Day [Holidays]"
    filtered = _filter_past_events(context, _NOW)
    assert "Mom's Birthday" in filtered
    assert "St. Patrick's Day" in filtered
    print("  \u2713 filter_keeps_allday_events")


def test_filter_removes_stale_imminent():
    """IMMINENT markers from a cached context are stale and should go."""
    context = 'IMMINENT: "Old Meeting" starts in 3 minutes\n\nUPCOMING:\n  (empty)'
    filtered = _filter_past_events(context, _NOW)
    assert "IMMINENT" not in filtered
    print("  \u2713 filter_removes_stale_imminent")


def test_filter_all_past_returns_empty():
    """If all events have passed, the result should be empty."""
    context = "UPCOMING:\n  7:00 AM \u2013 8:00 AM  \"Done Meeting\" [School]"
    filtered = _filter_past_events(context, _NOW)
    assert not filtered.strip() or "Done Meeting" not in filtered
    print("  \u2713 filter_all_past_returns_empty")


def test_all_past_events_omit_tool_from_prompt():
    """If all cached events have passed, filtering should return empty."""
    context = "UPCOMING:\n  7:00 AM \u2013 8:00 AM  \"Done\" [School]"
    filtered = _filter_past_events(context, _NOW)
    assert not filtered.strip(), f"Expected empty, got: {repr(filtered)}"
    print("  \u2713 all_past_events_omit_tool_from_prompt")


# --- Midnight Wrapping ---

def test_filter_keeps_midnight_wrapping_event():
    """Events that wrap past midnight (end time < start time) should be kept."""
    context = "UPCOMING:\n  10:00 PM \u2013 1:00 AM  \"Late Night Event\" [Personal]"
    filtered = _filter_past_events(context, _NOW)
    assert "Late Night Event" in filtered
    print("  \u2713 filter_keeps_midnight_wrapping_event")


# --- End Time Parsing ---

def test_extract_end_time_standard():
    """Should parse standard time range format."""
    line = '  10:00 AM \u2013 11:00 AM  "Meeting" (Room 204) [School]'
    result = _extract_end_time(line, _NOW)
    assert result is not None
    assert result.hour == 11
    assert result.minute == 0
    print("  \u2713 extract_end_time_standard")


def test_extract_end_time_pm():
    """Should handle PM times correctly."""
    line = '  2:00 PM \u2013 3:30 PM  "Dentist" [Personal]'
    result = _extract_end_time(line, _NOW)
    assert result is not None
    assert result.hour == 15
    assert result.minute == 30
    print("  \u2713 extract_end_time_pm")


def test_extract_end_time_midnight_wrap():
    """End time before start time should push to tomorrow."""
    line = '  10:00 PM \u2013 1:00 AM  "Party" [Personal]'
    result = _extract_end_time(line, _NOW)
    assert result is not None
    assert result > _NOW
    print("  \u2713 extract_end_time_midnight_wrap")


def test_extract_end_time_no_match():
    """Lines without time ranges should return None."""
    assert _extract_end_time("  Mom's Birthday [Personal]", _NOW) is None
    assert _extract_end_time("UPCOMING:", _NOW) is None
    assert _extract_end_time("", _NOW) is None
    print("  \u2713 extract_end_time_no_match")


# --- Agent Cycle Caching ---

def test_cache_tool_contexts():
    """_cache_tool_contexts should store contexts in tool_state."""
    from agent import _cache_tool_contexts

    mem = _make_memory()
    contexts = {
        "google_calendar": "UPCOMING:\n  3:00 PM \u2013 4:00 PM  \"Meeting\" [Work]",
        "schedule": "YOUR SCHEDULED PLAN:\n  #42  Today 15:00",
    }

    _cache_tool_contexts(mem, contexts)

    cached = mem.get_tool_state("google_calendar", "cached_context")
    assert cached is not None
    assert "Meeting" in cached

    sched_cached = mem.get_tool_state("schedule", "cached_context")
    assert sched_cached is None

    print("  \u2713 cache_tool_contexts")


# --- Multiple Tools ---

def test_multiple_tools_in_context():
    """Multiple cached tools should all appear in the output."""
    mem = _make_memory()
    _cache_context(mem, "google_calendar",
                   "UPCOMING:\n  2:00 PM \u2013 3:00 PM  \"Meeting\" [School]",
                   minutes_ago=10)
    # Gmail context doesn't have time-based lines, so no filtering issue
    _cache_context(mem, "gmail",
                   "NEW EMAILS:\n  From: john@example.com \u2014 Resume review",
                   minutes_ago=10)

    result = _load_tool_contexts_at_fixed_time(mem)
    assert "GOOGLE CALENDAR" in result
    assert "Meeting" in result
    assert "GMAIL" in result
    assert "Resume review" in result
    print("  \u2713 multiple_tools_in_context")


# --- Run all tests ---

if __name__ == "__main__":
    print("\nRunning tool context cache tests...\n")

    tests = [
        test_no_cached_tools,
        test_discover_cached_tools,
        test_cached_context_appears_in_prompt,
        test_freshness_just_updated,
        test_freshness_minutes_ago,
        test_freshness_hours_ago,
        test_filter_removes_past_events,
        test_filter_keeps_future_events,
        test_filter_keeps_allday_events,
        test_filter_removes_stale_imminent,
        test_filter_all_past_returns_empty,
        test_all_past_events_omit_tool_from_prompt,
        test_filter_keeps_midnight_wrapping_event,
        test_extract_end_time_standard,
        test_extract_end_time_pm,
        test_extract_end_time_midnight_wrap,
        test_extract_end_time_no_match,
        test_cache_tool_contexts,
        test_multiple_tools_in_context,
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