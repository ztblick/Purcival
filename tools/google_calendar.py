"""
GoogleCalendarTool — gives the agent awareness of Zach's calendar.

Reads events from all visible calendars (personal, school, shared,
birthdays, etc.) and presents them to the agent as formatted text.
The agent sees titles, times, locations, and which calendar each
event belongs to.

The tool handles its own state diffing — it tracks known events and
only surfaces what's new or changed. It also tracks what actions the
agent has already taken on each event (pre-meeting encouragement,
post-meeting debrief) so the agent doesn't repeat itself.

Error tracking: consecutive API failures are counted. After 3, the
agent is told to notify the user. After 10, the tool stops trying.

All state is persisted in the tool_state table via PersonaMemory.
"""

import json
import logging
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from tools.base import Tool, ToolMethod
from memory import PersonaMemory

logger = logging.getLogger(__name__)

# How far ahead to look for events (hours)
DEFAULT_LOOKAHEAD_HOURS = 24

# How many minutes before an event counts as "imminent"
IMMINENT_THRESHOLD_MINUTES = 15

# How often to refresh the calendar list (seconds)
CALENDAR_LIST_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Error tracking thresholds
FAILURE_NOTIFY_THRESHOLD = 3
FAILURE_DISABLE_THRESHOLD = 10

# Stale data pruning thresholds
EVENT_ACTIONS_MAX_AGE_DAYS = 7
KNOWN_EVENTS_MAX_AGE_HOURS = 48


class GoogleCalendarTool(Tool):

    name = "google_calendar"
    description = (
        "Read your Google Calendar. See upcoming events across all "
        "your calendars (personal, school, shared, etc.) with titles, "
        "times, and locations."
    )

    def __init__(self, memory: PersonaMemory, credentials):
        """
        Args:
            memory: The persona's memory instance for state persistence.
            credentials: A valid google.oauth2.credentials.Credentials object.
        """
        self.memory = memory
        self._credentials = credentials
        self._service = build("calendar", "v3", credentials=credentials)

    # --- Tool Interface ---

    def get_context(self) -> str | None:
        """
        Perception: fetch events, diff against known state, return
        what's new or relevant.

        Returns None if nothing has changed and nothing is imminent.
        Returns a formatted text block otherwise.

        Never calls an LLM. Pure API + diff logic.
        """
        # Check if we've hit the failure threshold
        failures = self._get_consecutive_failures()
        if failures >= FAILURE_DISABLE_THRESHOLD:
            return (
                "CALENDAR ERROR: Calendar access has failed "
                f"{failures} consecutive times. The last error was: "
                f"{self._get_last_error()}. "
                "Ask Zach to re-run the Google auth flow from the terminal."
            )

        try:
            # 1. Refresh calendar list if stale
            calendars = self._get_calendar_list()

            # 2. Fetch events across all calendars
            now = datetime.utcnow()
            time_min = now.isoformat() + "Z"
            time_max = (now + timedelta(hours=DEFAULT_LOOKAHEAD_HOURS)).isoformat() + "Z"

            all_events = self._fetch_all_events(calendars, time_min, time_max)

            # 3. Diff against known state
            known = self._load_known_events()
            event_actions = self._load_event_actions()
            diff = self._diff_events(all_events, known)

            # 4. Save updated known state
            self._save_known_events(all_events)

            # 5. Prune stale data
            self._prune_stale_data(event_actions)

            # 6. Reset failure counter on success
            self._reset_failures()

            # 7. Format the output
            return self._format_context(all_events, diff, event_actions)

        except Exception as e:
            self._record_failure(str(e))
            failures = self._get_consecutive_failures()
            logger.error(
                f"GoogleCalendarTool.get_context() failed "
                f"({failures} consecutive): {e}"
            )

            if failures >= FAILURE_NOTIFY_THRESHOLD:
                return (
                    f"CALENDAR ERROR: Calendar access has failed "
                    f"{failures} consecutive times. Latest error: {e}. "
                    "Let Zach know so he can investigate."
                )

            return None

    def get_methods(self) -> list[ToolMethod]:
        return [
            ToolMethod(
                name="get_upcoming",
                description=(
                    "Fetch upcoming calendar events with full details "
                    "(title, time, location, description, attendees). "
                    "Use this when you need more detail than the standard "
                    "context provides."
                ),
                tier="observe",
                parameters={
                    "hours": {
                        "type": "int",
                        "description": "How many hours ahead to look (default 24)",
                        "required": False,
                    },
                },
            ),
            # Future write methods:
            # ToolMethod(name="create_event", ..., tier="execute"),
            # ToolMethod(name="update_event", ..., tier="execute"),
        ]

    def execute(self, method_name: str, **kwargs) -> str:
        if method_name == "get_upcoming":
            return self._get_upcoming(kwargs.get("hours", DEFAULT_LOOKAHEAD_HOURS))
        else:
            raise ValueError(
                f"Unknown method '{method_name}' on GoogleCalendarTool"
            )

    # --- Calendar List Management ---

    def _get_calendar_list(self) -> list[dict]:
        """
        Get the list of visible calendars, refreshing if stale.

        Caches the calendar list in tool_state with a 24-hour TTL.
        Returns a list of dicts with 'id', 'summary', 'primary',
        'access_role', and 'selected'. Only calendars the user has
        toggled on in their Google Calendar view are included.
        """
        cached = self.memory.get_tool_state(self.name, "calendar_list")
        last_refreshed = self.memory.get_tool_state(
            self.name, "calendars_last_refreshed"
        )

        if cached and last_refreshed:
            try:
                age = (
                    datetime.now()
                    - datetime.strptime(last_refreshed, "%Y-%m-%d %H:%M:%S")
                ).total_seconds()
                if age < CALENDAR_LIST_TTL_SECONDS:
                    return json.loads(cached)
            except (ValueError, json.JSONDecodeError):
                pass  # Stale or corrupt — refresh

        # Fetch fresh calendar list
        result = self._service.calendarList().list().execute()
        items = result.get("items", [])

        calendars = []
        for cal in items:
            # Skip calendars the user has toggled off in their view.
            # The 'selected' field is False when a calendar is hidden
            # in the Google Calendar sidebar. Defaults to True if the
            # field is absent (primary calendars don't always have it).
            if not cal.get("selected", True):
                logger.debug(
                    f"Skipping hidden calendar: {cal.get('summary', cal['id'])}"
                )
                continue

            calendars.append({
                "id": cal["id"],
                "summary": cal.get("summary", cal["id"]),
                "primary": cal.get("primary", False),
                "access_role": cal.get("accessRole", "reader"),
                "selected": True,
            })

        # Cache it
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.memory.set_tool_state(
            self.name, "calendar_list", json.dumps(calendars)
        )
        self.memory.set_tool_state(
            self.name, "calendars_last_refreshed", now
        )

        logger.info(
            f"Calendar list refreshed: {len(calendars)} calendars visible"
        )
        return calendars

    # --- Event Fetching ---

    def _fetch_all_events(
        self,
        calendars: list[dict],
        time_min: str,
        time_max: str,
    ) -> list[dict]:
        """
        Fetch events from all visible calendars and merge into a
        single chronological list.

        Each event dict contains:
            id, summary, start, end, location, calendar_name,
            calendar_id, all_day, instance_id
        """
        all_events = []

        for cal in calendars:
            try:
                result = self._service.events().list(
                    calendarId=cal["id"],
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,  # Expand recurring events
                    orderBy="startTime",
                    maxResults=50,
                ).execute()

                for event in result.get("items", []):
                    parsed = self._parse_event(event, cal)
                    if parsed:
                        all_events.append(parsed)

            except Exception as e:
                # Log but continue — one bad calendar shouldn't block others
                logger.warning(
                    f"Failed to fetch events from '{cal['summary']}': {e}"
                )

        # Sort chronologically (all-day events first, then by start time)
        all_events.sort(key=lambda e: (
            not e["all_day"],  # All-day events first (False < True)
            e["sort_key"],
        ))

        return all_events

    def _parse_event(self, event: dict, calendar: dict) -> dict | None:
        """
        Parse a raw Google Calendar event into our internal format.

        Returns None for declined events or events without useful data.
        """
        # Skip declined events
        if event.get("status") == "cancelled":
            return None

        # Check if the user declined this event
        for attendee in event.get("attendees", []):
            if attendee.get("self") and attendee.get("responseStatus") == "declined":
                return None

        # Determine if all-day
        start = event.get("start", {})
        end = event.get("end", {})
        all_day = "date" in start and "dateTime" not in start

        if all_day:
            start_str = start.get("date", "")
            end_str = end.get("date", "")
            sort_key = start_str
        else:
            start_str = start.get("dateTime", "")
            end_str = end.get("dateTime", "")
            sort_key = start_str

        # Use the recurring event instance ID if available, otherwise
        # fall back to the event ID. This ensures each instance of a
        # recurring event gets its own identity for diffing.
        instance_id = event.get("id", "")

        return {
            "id": instance_id,
            "summary": event.get("summary", "(no title)"),
            "start": start_str,
            "end": end_str,
            "location": event.get("location", ""),
            "calendar_name": calendar["summary"],
            "calendar_id": calendar["id"],
            "all_day": all_day,
            "sort_key": sort_key,
            "description": event.get("description", ""),
            "attendees": [
                a.get("email", "") for a in event.get("attendees", [])
                if not a.get("self")
            ],
        }

    # --- Event Diffing ---

    def _diff_events(
        self,
        current: list[dict],
        known: list[dict],
    ) -> dict:
        """
        Compare current events against known state.

        Returns a dict with:
            new: list of events not in known
            changed: list of (event, changes_description) for modified events
            cancelled: list of events in known but not in current
            imminent: list of events starting within IMMINENT_THRESHOLD_MINUTES

        Events that have ended (their end time is in the past) are NOT
        reported as cancelled — they simply aged out of the query window.
        This prevents all-day events from yesterday being flagged as
        "cancelled" when they're just over.
        """
        known_by_id = {e["id"]: e for e in known}
        current_by_id = {e["id"]: e for e in current}

        now = datetime.utcnow()
        imminent_cutoff = now + timedelta(minutes=IMMINENT_THRESHOLD_MINUTES)

        new_events = []
        changed_events = []
        imminent_events = []

        for event in current:
            eid = event["id"]

            # Check if imminent (timed events only)
            if not event["all_day"] and event["start"]:
                try:
                    # Parse ISO datetime (may have timezone offset)
                    start_str = event["start"]
                    if start_str.endswith("Z"):
                        start_dt = datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        )
                        # Compare in UTC
                        cutoff_utc = imminent_cutoff.replace(
                            tzinfo=start_dt.tzinfo
                        )
                        now_utc = now.replace(tzinfo=start_dt.tzinfo)
                        if now_utc <= start_dt <= cutoff_utc:
                            minutes_away = int(
                                (start_dt - now_utc).total_seconds() / 60
                            )
                            imminent_events.append((event, minutes_away))
                    else:
                        start_dt = datetime.fromisoformat(start_str)
                        now_local = datetime.now(tz=start_dt.tzinfo)
                        cutoff_local = now_local + timedelta(
                            minutes=IMMINENT_THRESHOLD_MINUTES
                        )
                        if now_local <= start_dt <= cutoff_local:
                            minutes_away = int(
                                (start_dt - now_local).total_seconds() / 60
                            )
                            imminent_events.append((event, minutes_away))
                except (ValueError, TypeError):
                    pass  # Can't parse time — skip imminent check

            if eid not in known_by_id:
                new_events.append(event)
            else:
                # Check for changes
                old = known_by_id[eid]
                changes = []
                if old.get("summary") != event.get("summary"):
                    changes.append(
                        f"title changed from '{old.get('summary')}' "
                        f"to '{event.get('summary')}'"
                    )
                if old.get("start") != event.get("start"):
                    changes.append("time changed")
                if old.get("location") != event.get("location"):
                    changes.append("location changed")
                if changes:
                    changed_events.append((event, ", ".join(changes)))

        # Find cancelled events (in known but not in current).
        # Skip events that simply aged out of the query window —
        # an all-day event from yesterday or a timed event that ended
        # is not "cancelled," it's over.
        cancelled_events = []
        for eid in known_by_id:
            if eid in current_by_id:
                continue  # Still in the API response — not cancelled

            event = known_by_id[eid]
            end_str = event.get("end", "")

            # Check if the event has ended (and thus just aged out)
            try:
                if event.get("all_day"):
                    # All-day event: end date is exclusive in Google Calendar
                    # (an event on March 15 has end date March 16).
                    # If the end date is today or earlier, it's over.
                    from datetime import date as date_type
                    end_date = date_type.fromisoformat(end_str)
                    if end_date <= now.date():
                        continue  # All-day event has passed — not a cancellation
                elif end_str:
                    # Timed event: parse the ISO datetime
                    if end_str.endswith("Z"):
                        end_dt = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        )
                        if end_dt.replace(tzinfo=None) < now:
                            continue  # Timed event has ended
                    else:
                        end_dt = datetime.fromisoformat(end_str)
                        if end_dt.replace(tzinfo=None) < now:
                            continue  # Timed event has ended
            except (ValueError, TypeError):
                pass  # Can't parse end time — treat as genuinely cancelled

            cancelled_events.append(event)

        return {
            "new": new_events,
            "changed": changed_events,
            "cancelled": cancelled_events,
            "imminent": imminent_events,
        }

    # --- Context Formatting ---

    def _format_context(
        self,
        all_events: list[dict],
        diff: dict,
        event_actions: dict,
    ) -> str | None:
        """
        Format the calendar state into a text block for the LLM.

        Returns None if nothing noteworthy (no changes, no imminent events,
        and the agent has seen all current events before).
        """
        has_changes = (
            diff["new"]
            or diff["changed"]
            or diff["cancelled"]
            or diff["imminent"]
        )

        if not has_changes and not all_events:
            return None

        # Even without changes, if there are events we should report
        # them on planning cycles. The agent cycle determines whether
        # to include this based on tool loading logic.
        sections = []

        # Imminent events (timed only — all-day excluded by design)
        if diff["imminent"]:
            for event, minutes in diff["imminent"]:
                location = f" ({event['location']})" if event["location"] else ""
                actions = event_actions.get(event["id"], [])
                actions_str = self._format_event_actions(actions)
                sections.append(
                    f"IMMINENT: \"{event['summary']}\" starts in "
                    f"{minutes} minutes{location}\n"
                    f"  Calendar: {event['calendar_name']}\n"
                    f"  {actions_str}"
                )

        # New events
        if diff["new"]:
            for event in diff["new"]:
                if event["all_day"]:
                    continue  # All-day events shown in the main list
                time_str = self._format_time_range(event)
                location = f" ({event['location']})" if event["location"] else ""
                sections.append(
                    f"NEW: \"{event['summary']}\" added for "
                    f"{time_str}{location} [{event['calendar_name']}]"
                )

        # Changed events
        if diff["changed"]:
            for event, changes in diff["changed"]:
                sections.append(
                    f"CHANGED: \"{event['summary']}\" — {changes} "
                    f"[{event['calendar_name']}]"
                )

        # Cancelled events
        if diff["cancelled"]:
            for event in diff["cancelled"]:
                sections.append(
                    f"CANCELLED: \"{event['summary']}\" "
                    f"[{event['calendar_name']}]"
                )

        # All-day events
        all_day = [e for e in all_events if e["all_day"]]
        if all_day:
            all_day_lines = []
            for event in all_day:
                all_day_lines.append(
                    f"  {event['summary']} [{event['calendar_name']}]"
                )
            sections.append("ALL DAY:\n" + "\n".join(all_day_lines))

        # Timed events (the main schedule)
        timed = [e for e in all_events if not e["all_day"]]
        if timed:
            event_lines = []
            for event in timed:
                time_str = self._format_time_range(event)
                location = f" ({event['location']})" if event["location"] else ""
                cal_tag = f" [{event['calendar_name']}]"
                actions = event_actions.get(event["id"], [])
                actions_note = ""
                if actions:
                    actions_note = (
                        f"\n    Previous actions: "
                        f"{', '.join(a['action'] for a in actions)}"
                    )
                event_lines.append(
                    f"  {time_str}  \"{event['summary']}\""
                    f"{location}{cal_tag}{actions_note}"
                )
            sections.append("UPCOMING:\n" + "\n".join(event_lines))

        if not sections:
            return None

        return "\n\n".join(sections)

    def _format_time_range(self, event: dict) -> str:
        """Format an event's start-end time as a readable string."""
        try:
            start_dt = datetime.fromisoformat(event["start"])
            end_dt = datetime.fromisoformat(event["end"])
            start_str = start_dt.strftime("%I:%M %p").lstrip("0")
            end_str = end_dt.strftime("%I:%M %p").lstrip("0")

            # Include date if not today
            now = datetime.now(tz=start_dt.tzinfo)
            if start_dt.date() != now.date():
                date_str = start_dt.strftime("%a %m/%d ")
                return f"{date_str}{start_str} – {end_str}"

            return f"{start_str} – {end_str}"
        except (ValueError, TypeError):
            return f"{event['start']} – {event['end']}"

    def _format_event_actions(self, actions: list[dict]) -> str:
        """Format the action history for an event."""
        if not actions:
            return "Previous actions: (none — first engagement)"
        action_strs = [a["action"] for a in actions]
        return f"Previous actions: {', '.join(action_strs)}"

    # --- get_upcoming (explicit fetch with full detail) ---

    def _get_upcoming(self, hours: int = DEFAULT_LOOKAHEAD_HOURS) -> str:
        """
        Fetch upcoming events with full details (title, time, location,
        description, attendees). This is the explicit method the agent
        can call when it needs more than the standard context.
        """
        try:
            calendars = self._get_calendar_list()
            now = datetime.utcnow()
            time_min = now.isoformat() + "Z"
            time_max = (now + timedelta(hours=hours)).isoformat() + "Z"

            all_events = self._fetch_all_events(calendars, time_min, time_max)

            if not all_events:
                return f"No events in the next {hours} hours."

            lines = [f"Events in the next {hours} hours:\n"]

            for event in all_events:
                if event["all_day"]:
                    lines.append(
                        f"ALL DAY: {event['summary']} "
                        f"[{event['calendar_name']}]"
                    )
                else:
                    time_str = self._format_time_range(event)
                    lines.append(
                        f"{time_str}: {event['summary']} "
                        f"[{event['calendar_name']}]"
                    )

                if event["location"]:
                    lines.append(f"  Location: {event['location']}")
                if event["description"]:
                    # Truncate long descriptions
                    desc = event["description"][:300]
                    if len(event["description"]) > 300:
                        desc += "..."
                    lines.append(f"  Description: {desc}")
                if event["attendees"]:
                    lines.append(
                        f"  Attendees: {', '.join(event['attendees'][:10])}"
                    )
                lines.append("")  # Blank line between events

            return "\n".join(lines)

        except Exception as e:
            self._record_failure(str(e))
            return f"Error fetching calendar: {e}"

    # --- State Persistence ---

    def _load_known_events(self) -> list[dict]:
        """Load the last-known events from tool_state."""
        raw = self.memory.get_tool_state(self.name, "known_events")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return []

    def _save_known_events(self, events: list[dict]):
        """Save current events as the known state for next diff."""
        # Strip fields not needed for diffing to save space
        slim = []
        for e in events:
            slim.append({
                "id": e["id"],
                "summary": e["summary"],
                "start": e["start"],
                "end": e["end"],
                "location": e.get("location", ""),
                "calendar_name": e.get("calendar_name", ""),
                "all_day": e.get("all_day", False),
            })
        self.memory.set_tool_state(
            self.name, "known_events", json.dumps(slim)
        )

    def _load_event_actions(self) -> dict:
        """
        Load the event action history from tool_state.

        Returns a dict mapping event ID to a list of action records:
        {"event_id": [{"action": "pre_meeting_encouragement", "at": "..."}]}
        """
        raw = self.memory.get_tool_state(self.name, "event_actions")
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return {}

    def _save_event_actions(self, actions: dict):
        """Save the event action history."""
        self.memory.set_tool_state(
            self.name, "event_actions", json.dumps(actions)
        )

    def _prune_stale_data(self, event_actions: dict):
        """
        Remove old entries from event_actions and known_events.

        Event actions older than 7 days are pruned.
        Known events older than 48 hours are pruned.
        """
        now = datetime.now()
        cutoff = (now - timedelta(days=EVENT_ACTIONS_MAX_AGE_DAYS)).strftime(
            "%Y-%m-%d %H:%M"
        )

        pruned_actions = {}
        for event_id, actions in event_actions.items():
            recent = [a for a in actions if a.get("at", "") >= cutoff]
            if recent:
                pruned_actions[event_id] = recent

        if len(pruned_actions) != len(event_actions):
            self._save_event_actions(pruned_actions)

    # --- Error Tracking ---

    def _get_consecutive_failures(self) -> int:
        """Read the consecutive failure count from tool_state."""
        raw = self.memory.get_tool_state(self.name, "consecutive_failures")
        try:
            return int(raw) if raw else 0
        except (ValueError, TypeError):
            return 0

    def _get_last_error(self) -> str:
        """Read the last error message from tool_state."""
        return self.memory.get_tool_state(self.name, "last_error") or "unknown"

    def _record_failure(self, error: str):
        """Increment the failure counter and save the error."""
        failures = self._get_consecutive_failures() + 1
        self.memory.set_tool_state(
            self.name, "consecutive_failures", str(failures)
        )
        self.memory.set_tool_state(self.name, "last_error", error)

    def _reset_failures(self):
        """Reset the failure counter after a successful API call."""
        if self._get_consecutive_failures() > 0:
            self.memory.set_tool_state(
                self.name, "consecutive_failures", "0"
            )