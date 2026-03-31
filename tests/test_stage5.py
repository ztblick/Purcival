"""
Tests for Stage 5 — Agent Loop with Tools (JSON action format).

Tests cover:
    - New database tables and methods in PersonaMemory
    - Tool base classes
    - ScheduleTool operations (with internal validation)
    - JSON action parsing
    - Action validation (generic gate)
    - Schedule updates stripping and application (JSON format)
    - Agent cycle integration (without LLM)
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory import PersonaMemory


# =========================================================================
# Database Tests (unchanged — these test PersonaMemory, not parsing)
# =========================================================================

class TestToolState:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_get_nonexistent_key(self):
        assert self.memory.get_tool_state("calendar", "last_sync") is None

    def test_set_and_get(self):
        self.memory.set_tool_state("calendar", "last_sync", "2026-03-16 10:00:00")
        assert self.memory.get_tool_state("calendar", "last_sync") == "2026-03-16 10:00:00"

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
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_add_completed_action(self):
        action_id = self.memory.add_agent_action(
            cycle_id="abc123", tool_name="telegram",
            method_name="send_message", tier="message",
            parameters='{"text": "hello"}', result="sent", status="completed",
        )
        assert action_id > 0

    def test_today_action_count(self):
        assert self.memory.get_today_action_count() == 0
        self.memory.add_agent_action(cycle_id="abc", tool_name="telegram",
            method_name="send_message", tier="message", status="completed")
        assert self.memory.get_today_action_count() == 1
        self.memory.add_agent_action(cycle_id="abc", tool_name="calendar",
            method_name="get_upcoming", tier="observe", status="completed")
        assert self.memory.get_today_action_count() == 1  # observe doesn't count
        self.memory.add_agent_action(cycle_id="abc", tool_name="telegram",
            method_name="send_message", tier="message", status="failed")
        assert self.memory.get_today_action_count() == 1  # failed doesn't count

    def test_pending_proposals(self):
        assert self.memory.get_pending_proposals() == []
        self.memory.add_agent_action(
            cycle_id="abc", tool_name="gmail",
            method_name="send_email", tier="execute",
            parameters='{"to": "john@example.com"}', status="pending_approval",
        )
        proposals = self.memory.get_pending_proposals()
        assert len(proposals) == 1
        assert proposals[0]["tool_name"] == "gmail"

    def test_update_proposal_status(self):
        action_id = self.memory.add_agent_action(
            cycle_id="abc", tool_name="gmail",
            method_name="send_email", tier="execute", status="pending_approval",
        )
        self.memory.update_proposal_status(action_id, "approved")
        assert self.memory.get_pending_proposals() == []


class TestAgentNarrative:
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
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_add_log_entry(self):
        log_id = self.memory.add_reasoning_log(
            cycle_id="abc123", trigger_id=42,
            trigger_purpose="Morning planning", skipped=False, provider="claude",
        )
        assert log_id > 0

    def test_cleanup_old_data(self):
        self.memory.add_reasoning_log(cycle_id="recent", skipped=True)
        old_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn = self.memory._connect()
        conn.execute("INSERT INTO reasoning_log (cycle_id, skipped, created_at) VALUES (?, ?, ?)",
            ("old_entry", True, old_date))
        conn.commit()
        conn.close()
        self.memory.cleanup_old_data()
        conn = self.memory._connect()
        count = conn.execute("SELECT COUNT(*) as c FROM reasoning_log").fetchone()["c"]
        conn.close()
        assert count == 1


class TestScheduleConfigMigration:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_set_and_get_with_max_actions(self):
        self.memory.set_schedule_config("06:00", "23:00", 30, max_actions_per_day=50)
        assert self.memory.get_schedule_config()["max_actions_per_day"] == 50

    def test_default_max_actions(self):
        self.memory.set_schedule_config("06:00", "23:00", 30)
        assert self.memory.get_schedule_config()["max_actions_per_day"] == 25


class TestTriggerOperations:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_get_trigger(self):
        tid = self.memory.add_trigger("agent_cycle", "2026-12-25 10:00:00", '{"purpose": "test"}')
        trigger = self.memory.get_trigger(tid)
        assert trigger is not None
        assert trigger["type"] == "agent_cycle"

    def test_get_nonexistent_trigger(self):
        assert self.memory.get_trigger(9999) is None

    def test_update_trigger(self):
        tid = self.memory.add_trigger("agent_cycle", "2026-12-25 10:00:00", '{"purpose": "old"}')
        self.memory.update_trigger(tid, "2026-12-25 14:00:00", '{"purpose": "new"}')
        trigger = self.memory.get_trigger(tid)
        assert trigger["fire_at"] == "2026-12-25 14:00:00"
        assert trigger["context"] == '{"purpose": "new"}'


# =========================================================================
# JSON Action Parsing Tests (NEW — replaces freeform string parsing tests)
# =========================================================================

class TestJSONActionParsing:
    """Test the JSON-based action parsing."""

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

    def test_parse_empty_actions(self):
        from agent import _parse_actions_json
        actions, err = _parse_actions_json("[]")
        assert actions == []
        assert err is None

    def test_parse_single_action(self):
        from agent import _parse_actions_json
        text = '[{"tool": "telegram", "method": "send_message", "parameters": {"text": "Hello!"}}]'
        actions, err = _parse_actions_json(text)
        assert err is None
        assert len(actions) == 1
        assert actions[0]["tool"] == "telegram"
        assert actions[0]["method"] == "send_message"
        assert actions[0]["parameters"]["text"] == "Hello!"

    def test_parse_multiple_actions(self):
        from agent import _parse_actions_json
        text = json.dumps([
            {"tool": "telegram", "method": "send_message", "parameters": {"text": "Hi"}},
            {"tool": "schedule", "method": "add_wakeup", "parameters": {
                "time": "2026-03-16 11:05", "purpose": "Check in", "tools": ["telegram"]
            }},
        ])
        actions, err = _parse_actions_json(text)
        assert err is None
        assert len(actions) == 2
        assert actions[0]["tool"] == "telegram"
        assert actions[1]["tool"] == "schedule"
        assert actions[1]["parameters"]["purpose"] == "Check in"

    def test_parse_action_missing_parameters(self):
        """Actions without a parameters field should get an empty dict."""
        from agent import _parse_actions_json
        text = '[{"tool": "schedule", "method": "get_plan"}]'
        actions, err = _parse_actions_json(text)
        assert err is None
        assert len(actions) == 1
        assert actions[0]["parameters"] == {}

    def test_parse_invalid_json(self):
        from agent import _parse_actions_json
        actions, err = _parse_actions_json("not json at all")
        assert actions == []
        assert err is not None
        assert "JSON parse error" in err

    def test_parse_not_array(self):
        from agent import _parse_actions_json
        actions, err = _parse_actions_json('{"tool": "telegram"}')
        assert actions == []
        assert "must be a JSON array" in err

    def test_parse_missing_tool_field(self):
        from agent import _parse_actions_json
        text = '[{"method": "send_message", "parameters": {}}]'
        actions, err = _parse_actions_json(text)
        assert len(actions) == 0
        assert "missing 'tool'" in err

    def test_parse_missing_method_field(self):
        from agent import _parse_actions_json
        text = '[{"tool": "telegram", "parameters": {}}]'
        actions, err = _parse_actions_json(text)
        assert len(actions) == 0
        assert "missing 'method'" in err

    def test_parse_mixed_valid_and_invalid(self):
        """Valid actions should be parsed even if some are invalid."""
        from agent import _parse_actions_json
        text = json.dumps([
            {"tool": "telegram", "method": "send_message", "parameters": {"text": "Hi"}},
            {"method": "broken"},  # Missing tool
        ])
        actions, err = _parse_actions_json(text)
        assert len(actions) == 1
        assert actions[0]["tool"] == "telegram"
        assert err is not None  # Reports the error

    def test_parse_empty_string(self):
        from agent import _parse_actions_json
        actions, err = _parse_actions_json("")
        assert actions == []
        assert err is None

    def test_parse_none_text(self):
        """None or whitespace should return empty with no error."""
        from agent import _parse_actions_json
        actions, err = _parse_actions_json(None)
        assert actions == []
        assert err is None

    def test_parse_action_with_multiline_text(self):
        """JSON handles multiline strings naturally (via \\n)."""
        from agent import _parse_actions_json
        text = json.dumps([{
            "tool": "telegram",
            "method": "send_message",
            "parameters": {"text": "Line 1\nLine 2\nLine 3"}
        }])
        actions, err = _parse_actions_json(text)
        assert err is None
        assert "\n" in actions[0]["parameters"]["text"]

    def test_parse_schedule_action(self):
        """Schedule actions use the same format as any other tool."""
        from agent import _parse_actions_json
        text = json.dumps([{
            "tool": "schedule",
            "method": "add_wakeup",
            "parameters": {
                "time": "2026-03-17 09:00",
                "purpose": "Morning planning",
                "tools": ["google_calendar", "telegram"],
            }
        }])
        actions, err = _parse_actions_json(text)
        assert err is None
        assert actions[0]["tool"] == "schedule"
        assert actions[0]["parameters"]["tools"] == ["google_calendar", "telegram"]


# =========================================================================
# Action Validation Tests (generic gate — unchanged logic)
# =========================================================================

class TestActionValidation:
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

        self.tools = {"mock": MockTool(), "disabled": DisabledTool()}

    def test_valid_observe_action(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "read", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert valid and reason == "ok"

    def test_valid_message_action(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "send", "parameters": {}}
        valid, _ = _validate_action(action, self.tools, 0, 25)
        assert valid

    def test_execute_needs_approval(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "do_thing", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid and reason == "needs_approval"

    def test_unknown_tool(self):
        from agent import _validate_action
        action = {"tool": "nonexistent", "method": "read", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid and "unknown tool" in reason

    def test_disabled_tool(self):
        from agent import _validate_action
        action = {"tool": "disabled", "method": "read", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid and "disabled" in reason

    def test_unknown_method(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "nonexistent", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 0, 25)
        assert not valid and "unknown method" in reason

    def test_budget_exhausted(self):
        from agent import _validate_action
        action = {"tool": "mock", "method": "send", "parameters": {}}
        valid, reason = _validate_action(action, self.tools, 25, 25)
        assert not valid and "budget" in reason


# =========================================================================
# ScheduleTool Validation Tests (validation now lives inside the tool)
# =========================================================================

class TestScheduleToolValidation:
    """Test ScheduleTool's internal validation (moved from agent.py)."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")
        self.memory.set_schedule_config("06:00", "23:00", 30)
        from tools.schedule_tool import ScheduleTool
        self.tool = ScheduleTool(self.memory)

    def test_add_wakeup_valid(self):
        """Valid future time within operating hours should succeed."""
        now = datetime.now()
        if 6 <= now.hour <= 21:  # Only test when we can construct a valid time
            future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            result = self.tool.execute("add_wakeup", time=future, purpose="test", tools=["telegram"])
            assert "Scheduled" in result

    def test_add_wakeup_past_time_rejected(self):
        """Past times should raise ValueError."""
        try:
            self.tool.execute("add_wakeup", time="2020-01-01 10:00", purpose="test", tools=[])
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "past" in str(e).lower()

    def test_add_wakeup_outside_hours_rejected(self):
        """Times outside operating hours should raise ValueError."""
        future_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            self.tool.execute("add_wakeup", time=f"{future_date} 03:00", purpose="test", tools=[])
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "outside operating hours" in str(e).lower()

    def test_add_wakeup_unknown_tool_rejected(self):
        """Unknown tool names should raise ValueError."""
        now = datetime.now()
        if 6 <= now.hour <= 21:
            future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            try:
                self.tool.execute("add_wakeup", time=future, purpose="test", tools=["nonexistent"])
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "unknown tool" in str(e).lower()

    def test_cancel_nonexistent_trigger(self):
        """Cancelling a nonexistent trigger should raise ValueError."""
        try:
            self.tool.execute("cancel_wakeup", id=9999)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not found" in str(e).lower()

    def test_modify_nonexistent_trigger(self):
        """Modifying a nonexistent trigger should raise ValueError."""
        try:
            self.tool.execute("modify_wakeup", id=9999, purpose="new")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not found" in str(e).lower()

    def test_tool_name_normalization(self):
        """Tool names like 'telegram.send_message' should be stripped to 'telegram'."""
        now = datetime.now()
        if 6 <= now.hour <= 21:
            future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            result = self.tool.execute(
                "add_wakeup", time=future, purpose="test",
                tools=["telegram.send_message"]
            )
            assert "Scheduled" in result
            # Verify the stored context has normalized tool names
            triggers = self.memory.get_active_triggers()
            ctx = json.loads(triggers[-1]["context"])
            assert ctx["tools"] == ["telegram"]


class TestScheduleTool:
    """Test ScheduleTool's execute methods."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")
        self.memory.set_schedule_config("06:00", "23:00", 30)
        from tools.schedule_tool import ScheduleTool
        self.tool = ScheduleTool(self.memory)

    def test_add_and_get_plan(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        now = datetime.now()
        if 6 <= now.hour <= 21:
            result = self.tool.execute("add_wakeup", time=future, purpose="Test wake-up", tools=["telegram"])
            assert "Scheduled" in result
            plan = self.tool.execute("get_plan")
            assert "Test wake-up" in plan

    def test_cancel_wakeup(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        now = datetime.now()
        if 6 <= now.hour <= 21:
            result = self.tool.execute("add_wakeup", time=future, purpose="To cancel", tools=[])
            trigger_id = int(result.split("#")[1].split(" ")[0])
            cancel_result = self.tool.execute("cancel_wakeup", id=trigger_id)
            assert "Cancelled" in cancel_result

    def test_modify_wakeup(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        now = datetime.now()
        if 6 <= now.hour <= 21:
            result = self.tool.execute("add_wakeup", time=future, purpose="Original", tools=[])
            trigger_id = int(result.split("#")[1].split(" ")[0])
            new_time = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
            modify_result = self.tool.execute("modify_wakeup", id=trigger_id, time=new_time, purpose="Modified")
            assert "Modified" in modify_result

    def test_get_plan_empty(self):
        result = self.tool.execute("get_plan")
        assert "No upcoming" in result


# =========================================================================
# Schedule Updates Stripping Tests (updated for JSON format)
# =========================================================================

class TestScheduleUpdatesStripping:
    def test_no_tags(self):
        from agent import strip_schedule_updates
        clean, actions_json = strip_schedule_updates("Just a normal response.")
        assert clean == "Just a normal response."
        assert actions_json is None

    def test_with_json_tags(self):
        from agent import strip_schedule_updates
        response = (
            "Got it, I'll adjust my reminders.\n\n"
            "<schedule_updates>\n"
            '[{"tool": "schedule", "method": "modify_wakeup", '
            '"parameters": {"id": 42, "time": "2026-03-16 14:00"}},\n'
            ' {"tool": "schedule", "method": "cancel_wakeup", '
            '"parameters": {"id": 43}}]\n'
            "</schedule_updates>"
        )
        clean, actions_json = strip_schedule_updates(response)
        assert "adjust my reminders" in clean
        assert "<schedule_updates>" not in clean
        assert actions_json is not None
        # Verify it's valid JSON
        parsed = json.loads(actions_json)
        assert len(parsed) == 2
        assert parsed[0]["method"] == "modify_wakeup"
        assert parsed[1]["method"] == "cancel_wakeup"

    def test_tags_in_middle(self):
        from agent import strip_schedule_updates
        response = (
            "Sure thing!\n\n"
            "<schedule_updates>\n"
            '[{"tool": "schedule", "method": "add_wakeup", '
            '"parameters": {"time": "2026-03-17 09:00", "purpose": "test", "tools": ["telegram"]}}]\n'
            "</schedule_updates>\n\n"
            "Have a great day!"
        )
        clean, actions_json = strip_schedule_updates(response)
        assert "Sure thing!" in clean
        assert "great day" in clean
        assert "<schedule_updates>" not in clean
        assert actions_json is not None

    def test_apply_schedule_updates_valid(self):
        """apply_schedule_updates should execute valid schedule actions."""
        from agent import apply_schedule_updates
        tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(tmpdir)):
            mem = PersonaMemory("test_apply")
        mem.set_schedule_config("06:00", "23:00", 30)

        now = datetime.now()
        if 6 <= now.hour <= 21:
            future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            actions_json = json.dumps([{
                "tool": "schedule",
                "method": "add_wakeup",
                "parameters": {"time": future, "purpose": "Test", "tools": ["telegram"]},
            }])
            results = apply_schedule_updates(actions_json, mem)
            assert len(results) == 1
            assert results[0]["status"] == "applied"

    def test_apply_schedule_updates_rejects_non_schedule(self):
        """Non-schedule tools should be rejected in schedule_updates."""
        from agent import apply_schedule_updates
        tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(tmpdir)):
            mem = PersonaMemory("test_reject")

        actions_json = json.dumps([{
            "tool": "telegram",
            "method": "send_message",
            "parameters": {"text": "sneaky"},
        }])
        results = apply_schedule_updates(actions_json, mem)
        assert len(results) == 1
        assert results[0]["status"] == "rejected"
        assert "only schedule" in results[0]["reason"]

    def test_apply_schedule_updates_invalid_json(self):
        """Invalid JSON should be caught gracefully."""
        from agent import apply_schedule_updates
        tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(tmpdir)):
            mem = PersonaMemory("test_badjson")

        results = apply_schedule_updates("not json", mem)
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "parse error" in results[0]["reason"]


# =========================================================================
# Bootstrap Tests (unchanged)
# =========================================================================

class TestBootstrapLogic:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_no_schedule_no_bootstrap(self):
        from proactive import ensure_agent_has_plan
        ensure_agent_has_plan(self.memory)
        assert len(self.memory.get_active_triggers()) == 0

    def test_with_schedule_seeds_planning_cycle(self):
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        ensure_agent_has_plan(self.memory)
        triggers = self.memory.get_active_triggers()
        assert len(triggers) == 1
        ctx = json.loads(triggers[0]["context"])
        assert ctx["tools"] == []

    def test_existing_triggers_no_bootstrap(self):
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.memory.add_trigger("agent_cycle", future,
            context=json.dumps({"purpose": "test", "tools": []}))
        ensure_agent_has_plan(self.memory)
        assert len(self.memory.get_active_triggers()) == 1

    def test_bootstrap_idempotent(self):
        from proactive import ensure_agent_has_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        ensure_agent_has_plan(self.memory)
        ensure_agent_has_plan(self.memory)
        assert len(self.memory.get_active_triggers()) == 1


class TestContextScheduledPlan:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        with patch("memory.DATA_DIR", Path(self.tmpdir)):
            self.memory = PersonaMemory("test_persona")

    def test_no_plan_no_section(self):
        from context import _load_scheduled_plan
        assert _load_scheduled_plan(self.memory) == ""

    def test_with_plan_shows_triggers(self):
        from context import _load_scheduled_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.memory.add_trigger("agent_cycle", future,
            context=json.dumps({"purpose": "Remind Zach about lunch", "tools": ["telegram"]}))
        result = _load_scheduled_plan(self.memory)
        assert "Remind Zach about lunch" in result
        assert "schedule_updates" in result

    def test_schedule_updates_show_json_format(self):
        """The scheduled plan instructions should show JSON examples."""
        from context import _load_scheduled_plan
        self.memory.set_schedule_config("06:00", "23:00", 30, 25)
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.memory.add_trigger("agent_cycle", future,
            context=json.dumps({"purpose": "Test", "tools": ["telegram"]}))
        result = _load_scheduled_plan(self.memory)
        # Should show JSON format, not freeform function calls
        assert '"tool": "schedule"' in result
        assert '"method":' in result


# =========================================================================
# Truncation Detection Tests
# =========================================================================

class TestTruncationDetection:
    def test_complete_response(self):
        from agent import _is_response_truncated
        response = (
            "<reasoning>thinking</reasoning>\n"
            "<actions>[]</actions>\n"
            "<narrative_state>state</narrative_state>"
        )
        assert not _is_response_truncated(response)

    def test_missing_actions(self):
        from agent import _is_response_truncated
        response = "<reasoning>thinking</reasoning>"
        assert _is_response_truncated(response)

    def test_missing_narrative(self):
        from agent import _is_response_truncated
        response = (
            "<reasoning>thinking</reasoning>\n"
            "<actions>[]</actions>"
        )
        assert _is_response_truncated(response)

    def test_no_schedule_tag_needed(self):
        """The <schedule> tag no longer exists — should NOT be checked."""
        from agent import _is_response_truncated
        response = (
            "<reasoning>thinking</reasoning>\n"
            "<actions>[]</actions>\n"
            "<narrative_state>state</narrative_state>"
        )
        # This should be complete even without </schedule>
        assert not _is_response_truncated(response)


# =========================================================================
# Run all tests
# =========================================================================

if __name__ == "__main__":
    import traceback

    test_classes = [
        TestToolState,
        TestAgentActions,
        TestAgentNarrative,
        TestReasoningLog,
        TestScheduleConfigMigration,
        TestTriggerOperations,
        TestJSONActionParsing,
        TestActionValidation,
        TestScheduleToolValidation,
        TestScheduleTool,
        TestScheduleUpdatesStripping,
        TestBootstrapLogic,
        TestContextScheduledPlan,
        TestTruncationDetection,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            test_name = f"{cls.__name__}.{method_name}"
            try:
                if hasattr(instance, "setup_method"):
                    instance.setup_method()
                getattr(instance, method_name)()
                passed += 1
                print(f"  \u2713 {test_name}")
            except Exception as e:
                failed += 1
                errors.append((test_name, e))
                print(f"  \u2717 {test_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"\n  {name}:")
            traceback.print_exception(type(err), err, err.__traceback__)

    sys.exit(0 if failed == 0 else 1)