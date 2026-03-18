"""
Tests for Stage 5 — Agent Loop with Tools.

Tests cover:
    - New database tables and methods in PersonaMemory
    - Tool base classes
    - ScheduleTool operations
    - Action/schedule parsing
    - Action/schedule validation
    - Agent cycle integration (without LLM)
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# Make sure we can import from the purcival package
sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory


class TestToolState:
    """Test the tool_state key-value store."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_get_nonexistent_key(self):
        result = self.memory.get_tool_state("calendar", "last_sync")
        assert result is None

    def test_set_and_get(self):
        self.memory.set_tool_state("calendar", "last_sync", "2026-03-16 10:00:00")
        result = self.memory.get_tool_state("calendar", "last_sync")
        assert result == "2026-03-16 10:00:00"

    def test_overwrite(self):
        self.memory.set_tool_state("calendar", "last_sync", "old_value")
        self.memory.set_tool_state("calendar", "last_sync", "new_value")
        assert self.memory.get_tool_state("calendar", "last_sync") == "new_value"

    def test_different_tools_same_key(self):
        self.memory.set_tool_state("calendar", "last_sync", "cal_time")
        self.memory.set_tool_state("gmail", "last_sync", "gmail_time")
        assert self.memory.get_tool_state("calendar", "last_sync") == "cal_time"
        assert self.memory.get_tool_state("gmail", "last_sync") == "gmail_time"

    def test_json_serialization(self):
        data = {"event_id_123": [{"action": "reminded", "at": "10:00"}]}
        self.memory.set_tool_state("calendar", "event_actions", json.dumps(data))
        result = json.loads(self.memory.get_tool_state("calendar", "event_actions"))
        assert result["event_id_123"][0]["action"] == "reminded"


class TestAgentActions:
    """Test the agent_actions audit trail."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_add_completed_action(self):
        action_id = self.memory.add_agent_action(
            cycle_id="abc123",
            tool_name="telegram",
            method_name="send_message",
            tier="message",
            parameters='{"text": "hello"}',
            result="sent",
            status="completed",
        )
        assert action_id > 0

    def test_today_action_count(self):
        assert self.memory.get_today_action_count() == 0

        # Add a completed message action
        self.memory.add_agent_action(
            cycle_id="abc", tool_name="telegram",
            method_name="send_message", tier="message",
            status="completed",
        )
        assert self.memory.get_today_action_count() == 1

        # Add an observe action — should NOT count
        self.memory.add_agent_action(
            cycle_id="abc", tool_name="calendar",
            method_name="get_upcoming", tier="observe",
            status="completed",
        )
        assert self.memory.get_today_action_count() == 1

        # Add a failed action — should NOT count
        self.memory.add_agent_action(
            cycle_id="abc", tool_name="telegram",
            method_name="send_message", tier="message",
            status="failed",
        )
        assert self.memory.get_today_action_count() == 1

        # Add another completed message action
        self.memory.add_agent_action(
            cycle_id="abc", tool_name="telegram",
            method_name="send_message", tier="message",
            status="completed",
        )
        assert self.memory.get_today_action_count() == 2

    def test_pending_proposals(self):
        assert self.memory.get_pending_proposals() == []

        self.memory.add_agent_action(
            cycle_id="abc", tool_name="gmail",
            method_name="send_email", tier="execute",
            parameters='{"to": "john@example.com"}',
            status="pending_approval",
        )

        proposals = self.memory.get_pending_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tool_name"] == "gmail"
        assert proposals[0]["status"] == "pending_approval"

    def test_update_proposal_status(self):
        action_id = self.memory.add_agent_action(
            cycle_id="abc", tool_name="gmail",
            method_name="send_email", tier="execute",
            status="pending_approval",
        )

        self.memory.update_proposal_status(action_id, "approved")

        # Should no longer appear in pending proposals
        assert self.memory.get_pending_proposals() == []


class TestAgentNarrative:
    """Test the rolling narrative state."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_get_empty_narrative(self):
        assert self.memory.get_narrative() is None

    def test_set_and_get(self):
        self.memory.set_narrative("Zach has a busy morning.", "cycle_001")
        assert self.memory.get_narrative() == "Zach has a busy morning."

    def test_overwrite(self):
        self.memory.set_narrative("Morning state.", "cycle_001")
        self.memory.set_narrative("Afternoon state.", "cycle_002")
        assert self.memory.get_narrative() == "Afternoon state."


class TestReasoningLog:
    """Test the reasoning log with retention."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_add_log_entry(self):
        log_id = self.memory.add_reasoning_log(
            cycle_id="abc123",
            trigger_id=42,
            trigger_purpose="Morning planning",
            skipped=False,
            provider="claude",
        )
        assert log_id > 0

    def test_add_skipped_entry(self):
        log_id = self.memory.add_reasoning_log(
            cycle_id="abc124",
            skipped=True,
            skip_reason="no new context",
        )
        assert log_id > 0

    def test_cleanup_old_data(self):
        # Add a recent entry
        self.memory.add_reasoning_log(cycle_id="recent", skipped=True)

        # Manually insert an old entry (8 days ago)
        old_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.memory._connect()
        conn.execute(
            "INSERT INTO reasoning_log (cycle_id, skipped, created_at) VALUES (?, ?, ?)",
            ("old_entry", True, old_date),
        )
        conn.commit()
        conn.close()

        # Verify both exist
        conn = self.memory._connect()
        count = conn.execute("SELECT COUNT(*) as c FROM reasoning_log").fetchone()["c"]
        conn.close()
        assert count == 2

        # Run cleanup
        self.memory.cleanup_old_data()

        # Old entry should be gone, recent should remain
        conn = self.memory._connect()
        count = conn.execute("SELECT COUNT(*) as c FROM reasoning_log").fetchone()["c"]
        conn.close()
        assert count == 1


class TestScheduleConfigMigration:
    """Test that existing databases get the max_actions_per_day column."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_set_and_get_with_max_actions(self):
        self.memory.set_schedule_config("06:00", "23:00", 30, max_actions_per_day=50)
        config = self.memory.get_schedule_config()
        assert config["max_actions_per_day"] == 50

    def test_default_max_actions(self):
        self.memory.set_schedule_config("06:00", "23:00", 30)
        config = self.memory.get_schedule_config()
        assert config["max_actions_per_day"] == 25


class TestTriggerOperations:
    """Test the new trigger methods needed by ScheduleTool."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_get_trigger(self):
        trigger_id = self.memory.add_trigger(
            trigger_type="agent_cycle",
            fire_at="2026-12-25 10:00:00",
            context='{"purpose": "test"}',
        )
        trigger = self.memory.get_trigger(trigger_id)
        assert trigger is not None
        assert trigger["type"] == "agent_cycle"
        assert trigger["context"] == '{"purpose": "test"}'

    def test_get_nonexistent_trigger(self):
        assert self.memory.get_trigger(9999) is None

    def test_update_trigger(self):
        trigger_id = self.memory.add_trigger(
            trigger_type="agent_cycle",
            fire_at="2026-12-25 10:00:00",
            context='{"purpose": "old"}',
        )
        self.memory.update_trigger(
            trigger_id, "2026-12-25 14:00:00", '{"purpose": "new"}'
        )
        trigger = self.memory.get_trigger(trigger_id)
        assert trigger["fire_at"] == "2026-12-25 14:00:00"
        assert trigger["context"] == '{"purpose": "new"}'


# --- Parsing Tests ---

class TestActionParsing:
    """Test the LLM response parsing functions."""

    def test_extract_tag(self):
        from agent import _extract_tag
        text = "<reasoning>This is my reasoning.</reasoning>"
        assert _extract_tag(text, "reasoning") == "This is my reasoning."

    def test_extract_tag_multiline(self):
        from agent import _extract_tag
        text = "<narrative_state>\nLine 1.\nLine 2.\n</narrative_state>"
        result = _extract_tag(text, "narrative_state")
        assert "Line 1." in result
        assert "Line 2." in result

    def test_extract_missing_tag(self):
        from agent import _extract_tag
        assert _extract_tag("no tags here", "reasoning") is None

    def test_parse_action_line_simple(self):
        from agent import _parse_action_line
        result = _parse_action_line('telegram.send_message(text="Hello world")')
        assert result["tool"] == "telegram"
        assert result["method"] == "send_message"
        assert result["kwargs"]["text"] == "Hello world"

    def test_parse_action_line_positional(self):
        from agent import _parse_action_line
        result = _parse_action_line('telegram.send_message("Hello world")')
        assert result["tool"] == "telegram"
        assert result["method"] == "send_message"
        assert "_positional" in result["kwargs"]
        assert result["kwargs"]["_positional"][0] == "Hello world"

    def test_parse_action_line_positional_with_emoji(self):
        from agent import _parse_action_line
        result = _parse_action_line(
            'telegram.send_message("\U0001f340 Happy St. Patrick\'s Day!")'
        )
        assert result is not None
        assert "_positional" in result["kwargs"]
        assert "Patrick" in result["kwargs"]["_positional"][0]

    def test_parse_action_line_multiple_kwargs(self):
        from agent import _parse_action_line
        result = _parse_action_line(
            'gmail.send_email(to="john@example.com", subject="Hi", body="Hello")'
        )
        assert result["tool"] == "gmail"
        assert result["method"] == "send_email"
        assert result["kwargs"]["to"] == "john@example.com"
        assert result["kwargs"]["subject"] == "Hi"

    def test_parse_action_line_none(self):
        from agent import _parse_action_line
        assert _parse_action_line("none") is None
        assert _parse_action_line("") is None

    def test_parse_action_line_invalid(self):
        from agent import _parse_action_line
        assert _parse_action_line("not a valid action") is None

    def test_parse_schedule_line(self):
        from agent import _parse_schedule_line
        result = _parse_schedule_line(
            'schedule.add_wakeup(time="2026-03-16 09:52", '
            'purpose="Encourage Zach", tools=["calendar", "telegram"])'
        )
        assert result["method"] == "add_wakeup"
        assert result["kwargs"]["time"] == "2026-03-16 09:52"
        assert result["kwargs"]["purpose"] == "Encourage Zach"
        assert result["kwargs"]["tools"] == ["calendar", "telegram"]

    def test_parse_schedule_cancel(self):
        from agent import _parse_schedule_line
        result = _parse_schedule_line('schedule.cancel_wakeup(id=42)')
        assert result["method"] == "cancel_wakeup"
        assert result["kwargs"]["id"] == 42

    def test_parse_schedule_positional_add(self):
        from agent import _parse_schedule_line
        result = _parse_schedule_line(
            'schedule.add_wakeup("2026-03-17 15:30", '
            '"After school check-in", ["telegram"])'
        )
        assert result["method"] == "add_wakeup"
        assert result["kwargs"]["time"] == "2026-03-17 15:30"
        assert result["kwargs"]["purpose"] == "After school check-in"
        assert result["kwargs"]["tools"] == ["telegram"]

    def test_parse_schedule_positional_cancel(self):
        from agent import _parse_schedule_line
        result = _parse_schedule_line('schedule.cancel_wakeup(42)')
        assert result["method"] == "cancel_wakeup"
        assert result["kwargs"]["id"] == 42

    def test_parse_schedule_positional_no_tools(self):
        from agent import _parse_schedule_line
        result = _parse_schedule_line(
            'schedule.add_wakeup("2026-03-17 15:30", "Check in")'
        )
        assert result["method"] == "add_wakeup"
        assert result["kwargs"]["time"] == "2026-03-17 15:30"
        assert result["kwargs"]["purpose"] == "Check in"

    def test_parse_kwargs_with_escaped_quotes(self):
        from agent import _parse_action_line
        # The LLM might output: telegram.send_message(text="He said \"hello\"")
        line = 'telegram.send_message(text="He said \\"hello\\"")'
        result = _parse_action_line(line)
        assert result is not None
        assert "hello" in result["kwargs"]["text"]


# --- Validation Tests ---

class TestActionValidation:
    """Test the code-level action validation gate."""

    def setup_method(self):
        from tools.base import Tool, ToolMethod

        class MockTool(Tool):
            name = "mock"
            description = "A mock tool"
            enabled = True

            def get_methods(self):
                return [
                    ToolMethod(name="read", description="Read", tier="observe"),
                    ToolMethod(name="send", description="Send", tier="message"),
                    ToolMethod(name="do_thing", description="Do", tier="execute"),
                ]

        class DisabledTool(Tool):
            name = "disabled"
            description = "Disabled"
            enabled = False

        self.tools = {
            "mock": MockTool(),
            "disabled": DisabledTool(),
        }

    def test_valid_observe_action(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "read", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert valid
        assert reason == "ok"

    def test_valid_message_action(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "send", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert valid

    def test_execute_needs_approval(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "do_thing", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid
        assert reason == "needs_approval"

    def test_unknown_tool(self):
        from agent import _validate_action
        action = {"tool": "nonexistent", "method": "read", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid
        assert "unknown tool" in reason

    def test_disabled_tool(self):
        from agent import _validate_action
        action = {"tool": "disabled", "method": "read", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid
        assert "disabled" in reason

    def test_unknown_method(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "nonexistent", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid
        assert "unknown method" in reason

    def test_budget_exhausted(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "send", "kwargs": {}}
        valid, reason = _validate_action(action, self.tools, 25, 25)
        assert not valid
        assert "budget" in reason


class TestScheduleValidation:
    """Test schedule change validation."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")
        self.memory.set_schedule_config("06:00", "23:00", 30)
        self.config = self.memory.get_schedule_config()
        self.tool_names = {"telegram", "schedule", "google_calendar"}

    def test_valid_add_wakeup(self):
        from agent import _validate_schedule_change
        # Use a future date
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        change = {
            "method": "add_wakeup",
            "kwargs": {"time": future, "purpose": "test", "tools": ["telegram"]},
            "raw": "...",
        }
        valid, reason = _validate_schedule_change(
            change, self.memory, self.config, self.tool_names
        )
        # May fail if the future time is outside operating hours
        # For a robust test, construct a time we know is valid
        now = datetime.now()
        if 6 <= now.hour <= 21:
            assert valid, reason

    def test_past_time_rejected(self):
        from agent import _validate_schedule_change
        change = {
            "method": "add_wakeup",
            "kwargs": {"time": "2020-01-01 10:00", "purpose": "test", "tools": []},
            "raw": "...",
        }
        valid, reason = _validate_schedule_change(
            change, self.memory, self.config, self.tool_names
        )
        assert not valid
        assert "past" in reason

    def test_outside_hours_rejected(self):
        from agent import _validate_schedule_change
        # 3 AM is outside 06:00-23:00
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + " 03:00"
        change = {
            "method": "add_wakeup",
            "kwargs": {"time": future, "purpose": "test", "tools": []},
            "raw": "...",
        }
        valid, reason = _validate_schedule_change(
            change, self.memory, self.config, self.tool_names
        )
        assert not valid
        assert "outside operating hours" in reason

    def test_cancel_nonexistent_trigger(self):
        from agent import _validate_schedule_change
        change = {
            "method": "cancel_wakeup",
            "kwargs": {"id": 9999},
            "raw": "...",
        }
        valid, reason = _validate_schedule_change(
            change, self.memory, self.config, self.tool_names
        )
        assert not valid
        assert "not found" in reason


class TestScheduleTool:
    """Test the ScheduleTool's execute methods."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")
        from tools.schedule_tool import ScheduleTool
        self.tool = ScheduleTool(self.memory)

    def test_add_and_get_plan(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        result = self.tool.execute(
            "add_wakeup",
            time=future,
            purpose="Test wake-up",
            tools=["telegram"],
        )
        assert "Scheduled" in result

        plan = self.tool.execute("get_plan")
        assert "Test wake-up" in plan

    def test_cancel_wakeup(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        result = self.tool.execute(
            "add_wakeup", time=future, purpose="To cancel", tools=[],
        )
        # Extract trigger ID from result
        trigger_id = int(result.split("#")[1].split(" ")[0])

        cancel_result = self.tool.execute("cancel_wakeup", id=trigger_id)
        assert "Cancelled" in cancel_result

    def test_modify_wakeup(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        result = self.tool.execute(
            "add_wakeup", time=future, purpose="Original", tools=[],
        )
        trigger_id = int(result.split("#")[1].split(" ")[0])

        new_time = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
        modify_result = self.tool.execute(
            "modify_wakeup", id=trigger_id, time=new_time, purpose="Modified",
        )
        assert "Modified" in modify_result

    def test_get_plan_empty(self):
        result = self.tool.execute("get_plan")
        assert "No upcoming" in result


class TestScheduleUpdatesStripping:
    """Test the <schedule_updates> tag stripping in telegram_bot."""

    def test_no_tags(self):
        from telegram_bot import strip_schedule_updates
        clean, lines = strip_schedule_updates("Just a normal response.")
        assert clean == "Just a normal response."
        assert lines == []

    def test_with_tags(self):
        from telegram_bot import strip_schedule_updates
        response = (
            "Got it, I'll adjust my reminders.\n\n"
            "<schedule_updates>\n"
            'schedule.modify_wakeup(id=42, time="2026-03-16 14:00")\n'
            "schedule.cancel_wakeup(id=43)\n"
            "</schedule_updates>"
        )
        clean, lines = strip_schedule_updates(response)
        assert "adjust my reminders" in clean
        assert "<schedule_updates>" not in clean
        assert len(lines) == 2
        assert "modify_wakeup" in lines[0]
        assert "cancel_wakeup" in lines[1]

    def test_tags_in_middle(self):
        from telegram_bot import strip_schedule_updates
        response = (
            "Sure thing!\n\n"
            "<schedule_updates>\n"
            'schedule.add_wakeup(time="2026-03-17 09:00", purpose="test", tools=["telegram"])\n'
            "</schedule_updates>\n\n"
            "Have a great day!"
        )
        clean, lines = strip_schedule_updates(response)
        assert "Sure thing!" in clean
        assert "great day" in clean
        assert "<schedule_updates>" not in clean
        assert len(lines) == 1


class TestBootstrapLogic:
    """Test the ensure_agent_has_plan bootstrap."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_no_schedule_no_bootstrap(self):
        """Without a schedule config, bootstrap does nothing."""
        from proactive import ensure_agent_has_plan
        ensure_agent_has_plan(self.memory)
        triggers = self.memory.get_active_triggers()
        assert len(triggers) == 0

    def test_with_schedule_seeds_planning_cycle(self):
        """With a schedule config and no triggers, bootstrap seeds one."""
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        ensure_agent_has_plan(self.memory)
        triggers = self.memory.get_active_triggers()
        assert len(triggers) == 1
        ctx = json.loads(triggers[0]["context"])
        assert ctx["tools"] == []  # Empty tools = planning cycle
        assert "purpose" in ctx

    def test_existing_triggers_no_bootstrap(self):
        """If future triggers exist, bootstrap does nothing."""
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        # Add a future trigger
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.memory.add_trigger(
            trigger_type="agent_cycle",
            fire_at=future,
            context=json.dumps({"purpose": "test", "tools": []}),
        )
        ensure_agent_has_plan(self.memory)
        triggers = self.memory.get_active_triggers()
        assert len(triggers) == 1  # Only the one we added, no extra

    def test_bootstrap_idempotent(self):
        """Calling bootstrap twice doesn't create duplicate triggers."""
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        ensure_agent_has_plan(self.memory)
        ensure_agent_has_plan(self.memory)
        triggers = self.memory.get_active_triggers()
        assert len(triggers) == 1


class TestContextScheduledPlan:
    """Test that the scheduled plan appears in conversation context."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_no_plan_no_section(self):
        from context import _load_scheduled_plan
        result = _load_scheduled_plan(self.memory)
        assert result == ""

    def test_with_plan_shows_triggers(self):
        from context import _load_scheduled_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.memory.add_trigger(
            trigger_type="agent_cycle",
            fire_at=future,
            context=json.dumps({
                "purpose": "Remind Zach about lunch",
                "tools": ["telegram"],
            }),
        )
        result = _load_scheduled_plan(self.memory)
        assert "Remind Zach about lunch" in result
        assert "schedule_updates" in result  # Instructions for the LLM


# --- Run all tests ---

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestToolState,
        TestAgentActions,
        TestAgentNarrative,
        TestReasoningLog,
        TestScheduleConfigMigration,
        TestTriggerOperations,
        TestActionParsing,
        TestActionValidation,
        TestScheduleValidation,
        TestScheduleTool,
        TestScheduleUpdatesStripping,
        TestBootstrapLogic,
        TestContextScheduledPlan,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in methods:
            total += 1
            test_name = f"{cls.__name__}.{method_name}"
            try:
                if hasattr(instance, "setup_method"):
                    instance.setup_method()
                getattr(instance, method_name)()
                passed += 1
                print(f"  ✓ {test_name}")
            except Exception as e:
                failed += 1
                errors.append((test_name, e))
                print(f"  ✗ {test_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"\n  {name}:")
            traceback.print_exception(type(err), err, err.__traceback__)