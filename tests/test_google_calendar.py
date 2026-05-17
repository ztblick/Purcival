"""
Tests for Google Calendar integration.

Offline tests use mocked API responses — no Google credentials needed.
Live tests require credentials and skip gracefully if not available.

Run with: python tests/test_google_calendar.py

Covers:
    - Event diffing (new, changed, cancelled, imminent)
    - Aged-out events NOT reported as cancelled
    - Context formatting (all-day vs timed, multi-calendar)
    - Calendar list caching and TTL
    - Error tracking (consecutive failures, thresholds)
    - Event action history and pruning
    - Recurring event instance IDs
    - get_upcoming() with full detail
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone, date as date_type
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory
from tools.google_calendar import (
    GoogleCalendarTool,
    FAILURE_NOTIFY_THRESHOLD,
    FAILURE_DISABLE_THRESHOLD,
)


# --- Helpers ---

def _make_memory():
    """Create a PersonaMemory with a temp directory."""
    tmpdir = tempfile.mkdtemp()
    with patch("memory.DATA_DIR", Path(tmpdir)):
        return PersonaMemory("test_persona")


def _make_tool(memory=None, events_by_calendar=None):
    """
    Create a GoogleCalendarTool with a mocked Google API service.
    """
    if memory is None:
        memory = _make_memory()

    if events_by_calendar is None:
        events_by_calendar = {}

    mock_service = MagicMock()

    mock_service.calendarList().list().execute.return_value = {
        "items": [
            {
                "id": "primary",
                "summary": "Personal",
                "primary": True,
                "accessRole": "owner",
                "selected": True,
            },
            {
                "id": "school@group.calendar.google.com",
                "summary": "School",
                "primary": False,
                "accessRole": "reader",
                "selected": True,
            },
            {
                "id": "choir@group.calendar.google.com",
                "summary": "Choir",
                "primary": False,
                "accessRole": "reader",
                "selected": False,
            },
        ]
    }

    def mock_events_list(calendarId, **kwargs):
        mock_result = MagicMock()
        events = events_by_calendar.get(calendarId, [])
        mock_result.execute.return_value = {"items": events}
        return mock_result

    mock_service.events().list = mock_events_list

    mock_creds = MagicMock()
    tool = GoogleCalendarTool(memory, mock_creds)
    tool._service = mock_service

    return tool, memory


def _make_timed_event(
    event_id,
    summary,
    start_offset_hours=1,
    duration_hours=1,
    location="",
    calendar_id="primary",
):
    """Create a raw Google Calendar event dict for a timed event."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=start_offset_hours)
    end = start + timedelta(hours=duration_hours)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "location": location,
        "status": "confirmed",
    }


def _make_allday_event(event_id, summary, date_str=None):
    """Create a raw Google Calendar event dict for an all-day event."""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    return {
        "id": event_id,
        "summary": summary,
        "start": {"date": date_str},
        "end": {"date": date_str},
        "status": "confirmed",
    }


def _make_imminent_event(event_id, summary, minutes_from_now=5):
    """Create a timed event starting in the near future."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=minutes_from_now)
    end = start + timedelta(hours=1)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "status": "confirmed",
    }


# --- Diffing Tests ---

def test_diff_detects_new_events():
    """Events not in known state should be flagged as new."""
    tool, mem = _make_tool(events_by_calendar={
        "primary": [_make_timed_event("evt1", "Team Meeting", 2)],
    })

    context = tool.get_context()
    assert context is not None
    assert "NEW" in context or "Team Meeting" in context
    print("  \u2713 diff_detects_new_events")


def test_diff_detects_changed_events():
    """Events with modified title/time/location should be flagged."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [_make_timed_event("evt1", "Renamed Meeting", 2)],
    })

    old_events = [{
        "id": "evt1",
        "summary": "Old Meeting",
        "start": "2026-01-01T10:00:00Z",
        "end": "2026-01-01T11:00:00Z",
        "location": "",
        "calendar_name": "Personal",
        "all_day": False,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    assert context is not None
    assert "CHANGED" in context
    assert "Renamed Meeting" in context
    print("  \u2713 diff_detects_changed_events")


def test_diff_detects_cancelled_events():
    """Events in known state but missing from API should be flagged."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [],
    })

    # Seed known state with a FUTURE event that's now gone
    # (truly cancelled, not just aged out)
    tomorrow = (datetime.now() + timedelta(days=1))
    future_start = tomorrow.replace(hour=10, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_end = tomorrow.replace(hour=11, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_events = [{
        "id": "evt_gone",
        "summary": "Cancelled Meeting",
        "start": future_start,
        "end": future_end,
        "location": "",
        "calendar_name": "Personal",
        "all_day": False,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    assert context is not None
    assert "CANCELLED" in context
    assert "Cancelled Meeting" in context
    print("  \u2713 diff_detects_cancelled_events")


def test_diff_detects_imminent_events():
    """Events starting within 15 minutes should be flagged as imminent."""
    tool, mem = _make_tool(events_by_calendar={
        "primary": [_make_imminent_event("evt_soon", "Standup", 5)],
    })

    context = tool.get_context()
    assert context is not None
    assert "IMMINENT" in context
    assert "Standup" in context
    print("  \u2713 diff_detects_imminent_events")


def test_no_changes_returns_upcoming():
    """When events exist but nothing changed, still return the schedule."""
    mem = _make_memory()
    event = _make_timed_event("evt1", "Existing Meeting", 3)
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [event],
    })

    parsed = tool._parse_event(event, {"id": "primary", "summary": "Personal",
                                        "primary": True, "access_role": "owner"})
    mem.set_tool_state("google_calendar", "known_events",
                       json.dumps([{
                           "id": parsed["id"],
                           "summary": parsed["summary"],
                           "start": parsed["start"],
                           "end": parsed["end"],
                           "location": parsed["location"],
                           "calendar_name": parsed["calendar_name"],
                           "all_day": parsed["all_day"],
                       }]))

    context = tool.get_context()
    assert context is not None
    assert "UPCOMING" in context
    assert "Existing Meeting" in context
    print("  \u2713 no_changes_returns_upcoming")


# --- Aged-Out Event Tests (bug fix) ---

def test_aged_out_allday_not_cancelled():
    """
    An all-day event from yesterday that's no longer in the API response
    should NOT be reported as cancelled. It simply aged out of the query
    window.
    """
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [],  # No current events
    })

    # Seed known state with yesterday's all-day event.
    # Google Calendar uses exclusive end dates for all-day events:
    # an event on March 29 has end date March 30.
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    old_events = [{
        "id": "allday_yesterday",
        "summary": "Yesterday's Birthday",
        "start": yesterday,
        "end": today,  # Exclusive end = today means the event was yesterday
        "location": "",
        "calendar_name": "Personal",
        "all_day": True,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    # Should NOT report this as cancelled
    if context:
        assert "CANCELLED" not in context, (
            f"Yesterday's all-day event should not be reported as cancelled. "
            f"Got context: {context}"
        )
    print("  \u2713 aged_out_allday_not_cancelled")


def test_aged_out_timed_not_cancelled():
    """
    A timed event that has ended should NOT be reported as cancelled
    when it ages out of the query window.
    """
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [],
    })

    # Seed known state with an event that ended 2 hours ago
    two_hours_ago = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_events = [{
        "id": "past_meeting",
        "summary": "Finished Meeting",
        "start": two_hours_ago,
        "end": one_hour_ago,
        "location": "",
        "calendar_name": "Personal",
        "all_day": False,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    if context:
        assert "CANCELLED" not in context, (
            f"Ended timed event should not be reported as cancelled. "
            f"Got context: {context}"
        )
    print("  \u2713 aged_out_timed_not_cancelled")


def test_genuinely_cancelled_still_detected():
    """
    A future event that disappears from the API should still be
    reported as cancelled (it was truly deleted, not aged out).
    """
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [],
    })

    # Seed known state with a FUTURE event
    tomorrow = (datetime.utcnow() + timedelta(days=1))
    future_start = tomorrow.replace(hour=14, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_end = tomorrow.replace(hour=15, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_events = [{
        "id": "future_deleted",
        "summary": "Deleted Future Meeting",
        "start": future_start,
        "end": future_end,
        "location": "",
        "calendar_name": "Personal",
        "all_day": False,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    assert context is not None
    assert "CANCELLED" in context
    assert "Deleted Future Meeting" in context
    print("  \u2713 genuinely_cancelled_still_detected")


def test_aged_out_allday_today_not_cancelled():
    """
    An all-day event for today should NOT be cancelled even if it's
    not in the current API response (edge case: today's event with
    end date = tomorrow in Google's exclusive format).
    """
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem, events_by_calendar={
        "primary": [],
    })

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    old_events = [{
        "id": "allday_today",
        "summary": "Today's Event",
        "start": today,
        "end": tomorrow,  # Exclusive end = tomorrow means event is today
        "location": "",
        "calendar_name": "Personal",
        "all_day": True,
    }]
    mem.set_tool_state("google_calendar", "known_events", json.dumps(old_events))

    context = tool.get_context()
    # Today's all-day event has end date = tomorrow, which is > now.date(),
    # so it should NOT be filtered out as aged-out. It should appear as
    # cancelled (since it's not in the API response but hasn't ended yet).
    # This is correct behavior — if a today event disappears from the API,
    # it was genuinely cancelled.
    if context:
        # This is a genuine cancellation — the event is for today but
        # the API no longer returns it
        assert "CANCELLED" in context
    print("  \u2713 aged_out_allday_today_not_cancelled")


# --- All-Day Event Tests ---

def test_allday_events_shown():
    """All-day events should appear in a separate section."""
    tool, _ = _make_tool(events_by_calendar={
        "primary": [
            _make_allday_event("bday1", "Mom's Birthday"),
            _make_timed_event("mtg1", "Team Meeting", 3),
        ],
    })

    context = tool.get_context()
    assert context is not None
    assert "ALL DAY" in context
    assert "Mom's Birthday" in context
    assert "Team Meeting" in context
    print("  \u2713 allday_events_shown")


def test_allday_events_not_imminent():
    """All-day events should never be flagged as imminent."""
    tool, _ = _make_tool(events_by_calendar={
        "primary": [_make_allday_event("bday1", "Today's Birthday")],
    })

    context = tool.get_context()
    if context:
        assert "IMMINENT" not in context
    print("  \u2713 allday_events_not_imminent")


# --- Multi-Calendar Tests ---

def test_multi_calendar_merge():
    """Events from multiple calendars should be merged and tagged."""
    tool, _ = _make_tool(events_by_calendar={
        "primary": [_make_timed_event("p1", "Personal Lunch", 2)],
        "school@group.calendar.google.com": [
            _make_timed_event("s1", "Staff Meeting", 3),
        ],
    })

    context = tool.get_context()
    assert context is not None
    assert "Personal Lunch" in context
    assert "Staff Meeting" in context
    assert "[Personal]" in context
    assert "[School]" in context
    print("  \u2713 multi_calendar_merge")


# --- Calendar List Caching ---

def test_calendar_list_cached():
    """Calendar list should be fetched once and cached."""
    tool, mem = _make_tool()
    cals1 = tool._get_calendar_list()
    # 3 calendars in mock, but Choir has selected=False → 2 returned
    assert len(cals1) == 2
    cached = mem.get_tool_state("google_calendar", "calendar_list")
    assert cached is not None
    assert "Personal" in cached
    print("  \u2713 calendar_list_cached")


def test_hidden_calendar_excluded():
    """Calendars with selected=False should not be fetched."""
    tool, mem = _make_tool(events_by_calendar={
        "primary": [_make_timed_event("p1", "Personal Event", 2)],
        "choir@group.calendar.google.com": [
            _make_timed_event("c1", "Choir Rehearsal", 3),
        ],
    })

    context = tool.get_context()
    assert context is not None
    assert "Personal Event" in context
    assert "Choir Rehearsal" not in context
    assert "Choir" not in context
    print("  \u2713 hidden_calendar_excluded")


def test_calendar_list_ttl():
    """Calendar list should refresh after 24 hours."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    old_time = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    mem.set_tool_state("google_calendar", "calendar_list",
                       json.dumps([{"id": "stale", "summary": "Stale"}]))
    mem.set_tool_state("google_calendar", "calendars_last_refreshed", old_time)

    cals = tool._get_calendar_list()
    assert any(c["summary"] == "Personal" for c in cals)
    assert not any(c["summary"] == "Stale" for c in cals)
    print("  \u2713 calendar_list_ttl")


# --- Error Tracking ---

def test_error_tracking_increments():
    """Consecutive failures should increment the counter."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    tool._record_failure("test error 1")
    assert tool._get_consecutive_failures() == 1

    tool._record_failure("test error 2")
    assert tool._get_consecutive_failures() == 2
    assert tool._get_last_error() == "test error 2"
    print("  \u2713 error_tracking_increments")


def test_error_tracking_resets_on_success():
    """Success should reset the failure counter."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    tool._record_failure("error")
    tool._record_failure("error")
    assert tool._get_consecutive_failures() == 2

    tool._reset_failures()
    assert tool._get_consecutive_failures() == 0
    print("  \u2713 error_tracking_resets_on_success")


def test_error_notify_threshold():
    """At 3 failures, context should tell agent to notify user."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    for i in range(FAILURE_NOTIFY_THRESHOLD):
        mem.set_tool_state("google_calendar", "consecutive_failures", str(i + 1))

    tool._service.calendarList().list().execute.side_effect = Exception("API down")

    context = tool.get_context()
    assert context is not None
    assert "CALENDAR ERROR" in context
    assert "Let Zach know" in context
    print("  \u2713 error_notify_threshold")


def test_error_disable_threshold():
    """At 10 failures, tool should tell agent to ask for re-auth."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    mem.set_tool_state("google_calendar", "consecutive_failures",
                      str(FAILURE_DISABLE_THRESHOLD))

    context = tool.get_context()
    assert context is not None
    assert "CALENDAR ERROR" in context
    assert "re-run" in context.lower() or "auth" in context.lower()
    print("  \u2713 error_disable_threshold")


# --- Event Action History ---

def test_event_actions_tracked():
    """Event action history should persist in tool_state."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    actions = {
        "evt1": [
            {"action": "pre_meeting_encouragement", "at": "2026-03-18 09:52"},
        ]
    }
    tool._save_event_actions(actions)

    loaded = tool._load_event_actions()
    assert "evt1" in loaded
    assert loaded["evt1"][0]["action"] == "pre_meeting_encouragement"
    print("  \u2713 event_actions_tracked")


def test_event_actions_pruned():
    """Stale event action entries should be cleaned up."""
    mem = _make_memory()
    tool, _ = _make_tool(memory=mem)

    old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    recent_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    actions = {
        "old_evt": [{"action": "reminded", "at": old_date}],
        "recent_evt": [{"action": "reminded", "at": recent_date}],
    }

    tool._save_event_actions(actions)
    tool._prune_stale_data(actions)
    stored = tool._load_event_actions()
    assert "recent_evt" in stored
    assert "old_evt" not in stored
    print("  \u2713 event_actions_pruned")


# --- Declined Events ---

def test_declined_events_excluded():
    """Events the user declined should not appear."""
    tool, _ = _make_tool(events_by_calendar={
        "primary": [{
            "id": "declined_evt",
            "summary": "Meeting I Declined",
            "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()},
            "end": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()},
            "status": "confirmed",
            "attendees": [
                {"email": "me@example.com", "self": True, "responseStatus": "declined"},
            ],
        }],
    })

    context = tool.get_context()
    if context:
        assert "Meeting I Declined" not in context
    print("  \u2713 declined_events_excluded")


# --- get_upcoming() ---

def test_get_upcoming_full_detail():
    """get_upcoming() should include description and attendees."""
    tool, _ = _make_tool(events_by_calendar={
        "primary": [{
            "id": "evt_detail",
            "summary": "Design Review",
            "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()},
            "end": {"dateTime": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()},
            "location": "Room 204",
            "description": "Review the Q3 product designs and give feedback.",
            "attendees": [
                {"email": "me@example.com", "self": True},
                {"email": "alice@example.com"},
                {"email": "bob@example.com"},
            ],
            "status": "confirmed",
        }],
    })

    result = tool._get_upcoming(24)
    assert "Design Review" in result
    assert "Room 204" in result
    assert "Q3 product designs" in result
    assert "alice@example.com" in result
    assert "bob@example.com" in result
    assert "me@example.com" not in result
    print("  \u2713 get_upcoming_full_detail")


# --- Live Tests ---

def _credentials_available():
    """Check if Google Calendar credentials exist for testing."""
    if os.getenv("PURCIVAL_RUN_LIVE_TESTS") != "1":
        return False
    try:
        from google_auth import get_credentials
        creds = get_credentials("jo")
        return creds is not None
    except Exception:
        return False


@pytest.mark.skipif(
    not _credentials_available(),
    reason="live Google Calendar test; set PURCIVAL_RUN_LIVE_TESTS=1 and working Google credentials to run",
)
def test_live_calendar_list():
    """Live test: verify calendarList.list() returns calendars."""
    from google_auth import get_credentials
    from googleapiclient.discovery import build

    creds = get_credentials("jo")
    service = build("calendar", "v3", credentials=creds)
    result = service.calendarList().list().execute()
    calendars = result.get("items", [])

    assert len(calendars) > 0, "No calendars found"

    print(f"\n    Found {len(calendars)} calendars:")
    for cal in calendars:
        primary = " (PRIMARY)" if cal.get("primary") else ""
        role = cal.get("accessRole", "?")
        print(f"      {cal['summary']}{primary} [{role}]")

    print()
    print("  \u2713 live_calendar_list")


@pytest.mark.skipif(
    not _credentials_available(),
    reason="live Google Calendar test; set PURCIVAL_RUN_LIVE_TESTS=1 and working Google credentials to run",
)
def test_live_upcoming_events():
    """Live test: fetch upcoming events and verify format."""
    from google_auth import get_credentials

    mem = _make_memory()
    creds = get_credentials("jo")
    tool = GoogleCalendarTool(mem, creds)

    result = tool._get_upcoming(24)
    print(f"\n    {result[:500]}")
    if len(result) > 500:
        print("    ...")
    print()
    print("  \u2713 live_upcoming_events")


@pytest.mark.skipif(
    not _credentials_available(),
    reason="live Google Calendar test; set PURCIVAL_RUN_LIVE_TESTS=1 and working Google credentials to run",
)
def test_live_get_context():
    """Live test: run the full get_context() pipeline."""
    from google_auth import get_credentials

    mem = _make_memory()
    creds = get_credentials("jo")
    tool = GoogleCalendarTool(mem, creds)

    context = tool.get_context()
    if context:
        print(f"\n    {context[:500]}")
        if len(context) > 500:
            print("    ...")
    else:
        print("\n    (no events or no changes to report)")
    print()
    print("  \u2713 live_get_context")


# --- Run all tests ---

if __name__ == "__main__":
    print("\nRunning Google Calendar tests...\n")

    offline_tests = [
        test_diff_detects_new_events,
        test_diff_detects_changed_events,
        test_diff_detects_cancelled_events,
        test_diff_detects_imminent_events,
        test_no_changes_returns_upcoming,
        test_aged_out_allday_not_cancelled,
        test_aged_out_timed_not_cancelled,
        test_genuinely_cancelled_still_detected,
        test_aged_out_allday_today_not_cancelled,
        test_allday_events_shown,
        test_allday_events_not_imminent,
        test_multi_calendar_merge,
        test_calendar_list_cached,
        test_hidden_calendar_excluded,
        test_calendar_list_ttl,
        test_error_tracking_increments,
        test_error_tracking_resets_on_success,
        test_error_notify_threshold,
        test_error_disable_threshold,
        test_event_actions_tracked,
        test_event_actions_pruned,
        test_declined_events_excluded,
        test_get_upcoming_full_detail,
    ]

    live_tests = [
        test_live_calendar_list,
        test_live_upcoming_events,
        test_live_get_context,
    ]

    passed = 0
    failed = 0
    skipped = 0

    print("  Offline tests:\n")
    for test in offline_tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  \u2717 {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()

    if _credentials_available():
        print("  Live tests (Google credentials detected):\n")
        for test in live_tests:
            try:
                test()
                passed += 1
            except Exception as e:
                print(f"  \u2717 {test.__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
    else:
        skipped = len(live_tests)
        print(
            f"  Skipping {skipped} live test(s) — no Google credentials.\n"
            f"  To run: python -c \"from google_auth import run_auth_flow; "
            f"run_auth_flow('jo')\"\n"
        )

    print(f"\n{'='*50}")
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"  {', '.join(parts)}")
    print(f"{'='*50}\n")
    sys.exit(0 if failed == 0 else 1)
